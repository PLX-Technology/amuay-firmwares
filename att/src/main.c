/*
 * ATT (Varec 2500): lee el encoder en cuadratura y transmite la medida por SPE
 * hasta la power switch.
 *
 * ENCODER — POR QUE ES POR SOFTWARE Y NO CON TIM2:
 *   El esquematico llama a los nets PA0="TIM2_CH1" (ENCODER AA) y PA1="TIM2_CH2"
 *   (ENCODER AB), pero en el STM32WBA65 **TIM2_CH1 NO EXISTE EN PA0** (solo en
 *   PA5/PA11/PB12, todos ya ocupados: NSS del ADIN2111, RS485, consola).
 *   Ningun timer tiene un par de cuadratura CH1+CH2 en PA0+PA1:
 *     TIM2 CH1={PA5,PA11,PB12} CH2={PA1,PA8,PC4}   -> PA0 no
 *     TIM3 CH1={PA10,PA2,PC3,PE3} CH2={PA1,PA9,PC4} -> PA0 solo da CH3
 *     TIM1 CH1={PA11,PB11,PB8} CH2={PA12,PA15,PA8}  -> ninguno
 *     LPTIM1 IN1=PA0 pero IN2=PB3 unicamente        -> PA1 no
 *   => decodificacion por SOFTWARE con interrupciones GPIO (PA0=EXTI0, PA1=EXTI1,
 *      lineas distintas, no chocan). El flotador de un Varec 2500 se mueve lento,
 *      asi que sobra de rapido. Mismo criterio que el SPI bit-bang del ADIN2111.
 *
 * BYPASS: PD14 (UC_BYPASS_EN) en ALTO energiza el rele K1 y mete el ADIN2111 en
 * la linea SPE. Sin esto NO hay link jamas (el rele arranca puenteando P1<->P2).
 */
#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/sys/ring_buffer.h>
#include <zephyr/net/net_if.h>
#include <zephyr/net/ethernet.h>

#ifndef ETH_P_ALL
#define ETH_P_ALL 0x0003
#endif
#include <zephyr/net/socket.h>
#include <zephyr/net/net_mgmt.h>
#include <zephyr/net/net_event.h>
#include <zephyr/modbus/modbus.h>
#include <zephyr/drivers/eeprom.h>
#include <zephyr/sys/crc.h>
#include <errno.h>
#include <zephyr/net/phy.h>
#include <zephyr/logging/log.h>
LOG_MODULE_REGISTER(att, LOG_LEVEL_INF);

static const struct device *const adin = DEVICE_DT_GET(DT_NODELABEL(adin2111));
static const struct device *const gpa  = DEVICE_DT_GET(DT_NODELABEL(gpioa));
static const struct device *const gpd  = DEVICE_DT_GET(DT_NODELABEL(gpiod));

#define BYPASS_EN_PIN 14   /* PD14 = UC_BYPASS_EN */
#define ENC_A_PIN      0   /* PA0  = ENCODER AA */
#define ENC_B_PIN      1   /* PA1  = ENCODER AB */

/* ---------------- decodificador de cuadratura por software ---------------- */
static struct gpio_callback enc_cb;
static volatile int32_t  enc_count;      /* posicion (x4) */
static volatile uint32_t enc_edges;      /* flancos vistos: prueba de vida del cableado */
static volatile uint32_t enc_errors;     /* transiciones ilegales = pulso perdido/rebote */
static volatile uint8_t  enc_prev;       /* estado previo (A<<1|B) */

/* Tabla de cuadratura x4: indice = (prev<<2)|cur -> -1, 0, +1, o 2 = ilegal */
static const int8_t QTAB[16] = {
	 0, +1, -1,  2,
	-1,  0,  2, +1,
	+1,  2,  0, -1,
	 2, -1, +1,  0,
};

/* ---------------- RS-485 (USART2, PA12=TX PA11=RX) ---------------- */
static volatile uint32_t r485_rx_bytes;

RING_BUF_DECLARE(r485_rb, 256);

/* ISR: vacia el FIFO del UART al ring buffer. Imprescindible: con uart_poll_in()
 * cada 500ms se perdian todos los bytes menos el ultimo (a 115200 un mensaje de
 * 21 bytes dura 1.8ms y el registro RX solo guarda uno). */



/* DHCP: el servidor es la TPU (dnsmasq via NetworkManager, 192.168.50.1/24).
 * La IP es para gestion/diagnostico; el encoder sigue en tramas L2 (0x88B5). */
static struct net_mgmt_event_callback dhcp_cb;

static void dhcp_handler(struct net_mgmt_event_callback *cb,
			uint64_t mgmt_event, struct net_if *iface)
{
	char buf[NET_IPV4_ADDR_LEN];

	if (mgmt_event != NET_EVENT_IPV4_ADDR_ADD) { return; }
	for (int i = 0; i < NET_IF_MAX_IPV4_ADDR; i++) {
		struct net_if_addr *ia = &iface->config.ip.ipv4->unicast[i].ipv4;

		if (ia->addr_type != NET_ADDR_DHCP) { continue; }
		LOG_INF("DHCP OK en iface %d: IP=%s", net_if_get_by_iface(iface),
			net_addr_ntop(AF_INET, &ia->address.in_addr, buf, sizeof(buf)));
	}
}

/* =================== CONFIGURACION PERSISTENTE (EEPROM) ===================
 * Se guarda en la AT24C64 (I2C1, 0x50). Permite cambiar el modo, la unit id
 * de Modbus y los baudios SIN recompilar ni destapar el sensor: en campo van
 * varios Varec en el mismo bus RS-485 y cada uno necesita su propia unit id.
 *
 * Se configura desde la CM5 por SPE, escribiendo los HOLDING REGISTERS por
 * Modbus TCP. Los cambios se guardan con el comando 0xA5 y se aplican en el
 * siguiente arranque (comportamiento habitual en equipo industrial).
 *
 * ★ El servidor Modbus TCP NO se puede apagar: es el canal de configuracion.
 *   Si se pudiera, un cambio equivocado dejaria el sensor inalcanzable.
 * ====================================================================== */
#define CFG_MAGIC   0x41545431u   /* "ATT1" */
#define CFG_ADDR    0

struct att_cfg {
	uint32_t magic;
	uint16_t flags;      /* bit0 = push L2 0x88B5 | bit1 = Modbus RTU */
	uint16_t unit_id;    /* 1..247 */
	uint16_t baud_div;   /* baudios/100: 96, 192, 384, 1152 */
	uint16_t parity;     /* 0=none 1=even 2=odd */
	uint16_t push_ms10;  /* periodo del push L2, en decenas de ms */
	uint16_t crc;
} __packed;

#define CFG_F_PUSH   BIT(0)
#define CFG_F_RTU    BIT(1)

static const struct device *const eep = DEVICE_DT_GET(DT_NODELABEL(eeprom0));
static struct att_cfg cfg;

static void cfg_defaults(void)
{
	cfg.magic = CFG_MAGIC;
	cfg.flags = CFG_F_PUSH | CFG_F_RTU;   /* el push L2 es el modo PRINCIPAL */
	cfg.unit_id = 1;
	cfg.baud_div = 192;                   /* 19200 = default del estandar Modbus */
	cfg.parity = 1;                       /* even */
	cfg.push_ms10 = 50;                   /* 500 ms */
}

static uint16_t cfg_crc(const struct att_cfg *c)
{
	return crc16_ansi((const uint8_t *)c, sizeof(*c) - sizeof(uint16_t));
}

static void cfg_load(void)
{
	struct att_cfg t;

	if (!device_is_ready(eep)) {
		LOG_WRN("EEPROM no lista: uso la configuracion por defecto");
		cfg_defaults();
		return;
	}
	if (eeprom_read(eep, CFG_ADDR, &t, sizeof(t)) < 0) {
		LOG_WRN("EEPROM ilegible: configuracion por defecto");
		cfg_defaults();
		return;
	}
	if (t.magic != CFG_MAGIC || t.crc != cfg_crc(&t)) {
		LOG_INF("EEPROM sin configuracion valida: escribo la de fabrica");
		cfg_defaults();
		cfg.crc = cfg_crc(&cfg);
		eeprom_write(eep, CFG_ADDR, &cfg, sizeof(cfg));
		return;
	}
	cfg = t;
	LOG_INF("Config leida de EEPROM: flags=0x%04x unit_id=%u baud=%u parity=%u push=%ums",
		cfg.flags, cfg.unit_id, cfg.baud_div * 100u, cfg.parity, cfg.push_ms10 * 10u);
}

static int cfg_save(void)
{
	int r;

	if (!device_is_ready(eep)) { return -ENODEV; }
	cfg.magic = CFG_MAGIC;
	cfg.crc = cfg_crc(&cfg);
	r = eeprom_write(eep, CFG_ADDR, &cfg, sizeof(cfg));
	LOG_INF("Config guardada en EEPROM (ret=%d). Se aplica al reiniciar.", r);
	return r;
}

/* ================= Modbus: la ATT es ESCLAVO (servidor) =================
 * Mismo mapa de registros por los dos caminos:
 *   - RTU  sobre RS-485 (usart2 -> ADM2587E), unit id 1
 *   - TCP  sobre SPE, puerto 502
 *
 * Se usan INPUT REGISTERS (FC 04): son de solo lectura, que es lo que
 * semanticamente corresponde a una medida.
 *
 *   IR 0-1 : posicion del encoder (int32, palabra alta primero)
 *   IR 2-3 : flancos totales (uint32)
 *   IR 4   : transiciones ilegales (rebotes / pulsos perdidos)
 *   IR 5   : uptime en segundos
 *   IR 6   : estado (bit0 = portadora SPE if1, bit1 = if2)
 * ====================================================================== */
#define MB_UNIT_ID   1
#define MB_TCP_PORT  502

static int mb_input_reg_rd(uint16_t addr, uint16_t *reg)
{
	uint32_t v;

	switch (addr) {
	case 0: *reg = (uint16_t)((uint32_t)enc_count >> 16); break;
	case 1: *reg = (uint16_t)((uint32_t)enc_count & 0xFFFF); break;
	case 2: *reg = (uint16_t)(enc_edges >> 16); break;
	case 3: *reg = (uint16_t)(enc_edges & 0xFFFF); break;
	case 4: *reg = (uint16_t)enc_errors; break;
	case 5: *reg = (uint16_t)(k_uptime_get() / 1000); break;
	case 6:
		v = 0;
		if (net_if_is_carrier_ok(net_if_get_by_index(1))) { v |= BIT(0); }
		if (net_if_is_carrier_ok(net_if_get_by_index(2))) { v |= BIT(1); }
		*reg = (uint16_t)v;
		break;
	default:
		return -ENOTSUP;   /* -> el esclavo responde ILLEGAL DATA ADDRESS */
	}
	return 0;
}

/* --- HOLDING REGISTERS = configuracion (FC 03 leer / FC 06 escribir) ---
 *   HR 0 : flags (bit0 = push L2, bit1 = Modbus RTU)
 *   HR 1 : unit id Modbus (1..247)
 *   HR 2 : baudios RTU / 100 (96, 192, 384, 1152)
 *   HR 3 : paridad RTU (0=none 1=even 2=odd)
 *   HR 4 : periodo del push L2, en decenas de ms
 *   HR 9 : comando -> 0xA5 = guardar en EEPROM, 0x5A = valores de fabrica
 */
static int mb_holding_rd(uint16_t addr, uint16_t *reg)
{
	switch (addr) {
	case 0: *reg = cfg.flags; break;
	case 1: *reg = cfg.unit_id; break;
	case 2: *reg = cfg.baud_div; break;
	case 3: *reg = cfg.parity; break;
	case 4: *reg = cfg.push_ms10; break;
	case 9: *reg = 0; break;
	default: return -ENOTSUP;
	}
	return 0;
}

static int mb_holding_wr(uint16_t addr, uint16_t reg)
{
	switch (addr) {
	case 0: cfg.flags = reg & (CFG_F_PUSH | CFG_F_RTU); break;
	case 1:
		if (reg < 1 || reg > 247) { return -ENOTSUP; }   /* fuera del rango Modbus */
		cfg.unit_id = reg;
		break;
	case 2:
		if (reg != 96 && reg != 192 && reg != 384 && reg != 1152) { return -ENOTSUP; }
		cfg.baud_div = reg;
		break;
	case 3:
		if (reg > 2) { return -ENOTSUP; }
		cfg.parity = reg;
		break;
	case 4:
		if (reg < 5 || reg > 6000) { return -ENOTSUP; }  /* 50ms .. 60s */
		cfg.push_ms10 = reg;
		break;
	case 9:
		if (reg == 0xA5) { return cfg_save() < 0 ? -EIO : 0; }
		if (reg == 0x5A) { cfg_defaults(); return cfg_save() < 0 ? -EIO : 0; }
		return -ENOTSUP;
	default: return -ENOTSUP;
	}
	return 0;
}

static struct modbus_user_callbacks mb_cbs = {
	.input_reg_rd = mb_input_reg_rd,
	.holding_reg_rd = mb_holding_rd,
	.holding_reg_wr = mb_holding_wr,
};

/* ---- RTU sobre RS-485 ---- */
static struct modbus_iface_param mb_rtu = {
	.mode = MODBUS_MODE_RTU,
	.server = { .user_cb = &mb_cbs, .unit_id = MB_UNIT_ID },
	.serial = {
		.baud = 19200,
		.parity = UART_CFG_PARITY_EVEN,   /* 19200 8E1 = el default del estandar */
		.stop_bits = UART_CFG_STOP_BITS_1,
	},
};

/* ---- TCP sobre SPE: Zephyr no trae servidor, se usa RAW ADU ---- */
static int mb_tcp_iface = -1;
static volatile int mb_tcp_state;   /* 0=sin arrancar 1=escuchando -N=errno */
static volatile int mb_tcp_conns;
static int mb_tcp_client = -1;

/* El callback del nucleo de Modbus SOLO copia y avisa. NO envia por el socket:
 * se ejecuta en el contexto del nucleo de Modbus, y hacer E/S de red ahi (con
 * buffers grandes en su pila) tumbaba la placa. El envio lo hace el hilo TCP.
 * Es el patron del sample oficial samples/subsys/modbus/tcp_server. */
static struct modbus_adu tmp_adu;
K_SEM_DEFINE(mb_resp, 0, 1);

static int mb_raw_tx(const int iface, const struct modbus_adu *adu, void *user_data)
{
	tmp_adu.trans_id = adu->trans_id;
	tmp_adu.proto_id = adu->proto_id;
	tmp_adu.length   = adu->length;
	tmp_adu.unit_id  = adu->unit_id;
	tmp_adu.fc       = adu->fc;
	memcpy(tmp_adu.data, adu->data, MIN(adu->length, CONFIG_MODBUS_BUFFER_SIZE));
	k_sem_give(&mb_resp);
	return 0;
}

static struct modbus_iface_param mb_raw = {
	.mode = MODBUS_MODE_RAW,
	.server = { .user_cb = &mb_cbs, .unit_id = MB_UNIT_ID },
	.rawcb = { .raw_tx_cb = mb_raw_tx, .user_data = NULL },
};

/* Encuadre MBAP con los helpers de Zephyr (modbus_raw_get_header /
 * modbus_raw_put_header). Armarlo a mano estaba MAL: el codigo de funcion (fc)
 * es un campo propio del ADU, no parte de data -> todo iba corrido un byte.
 * MSG_WAITALL: sin el, un TCP fragmentado rompe el encuadre. */
static int mb_tcp_reply(int client, struct modbus_adu *adu)
{
	uint8_t header[MODBUS_MBAP_AND_FC_LENGTH];

	modbus_raw_put_header(adu, header);
	if (zsock_send(client, header, sizeof(header), 0) < 0) { return -errno; }
	if (adu->length && zsock_send(client, adu->data, adu->length, 0) < 0) { return -errno; }
	return 0;
}

static int mb_tcp_txn(int client)
{
	uint8_t header[MODBUS_MBAP_AND_FC_LENGTH];
	int rc;

	rc = zsock_recv(client, header, sizeof(header), ZSOCK_MSG_WAITALL);
	if (rc <= 0) { return rc == 0 ? -ENOTCONN : -errno; }

	modbus_raw_get_header(&tmp_adu, header);
	if (tmp_adu.length > CONFIG_MODBUS_BUFFER_SIZE) { return -EMSGSIZE; }

	if (tmp_adu.length) {
		rc = zsock_recv(client, tmp_adu.data, tmp_adu.length, ZSOCK_MSG_WAITALL);
		if (rc <= 0) { return rc == 0 ? -ENOTCONN : -errno; }
	}

	if (modbus_raw_submit_rx(mb_tcp_iface, &tmp_adu)) {
		LOG_ERR("Modbus TCP: submit_rx fallo");
		return -EIO;
	}
	if (k_sem_take(&mb_resp, K_MSEC(1000)) != 0) {
		LOG_ERR("Modbus TCP: sin respuesta del nucleo");
		modbus_raw_set_server_failure(&tmp_adu);
	}
	return mb_tcp_reply(client, &tmp_adu);
}

static void mb_tcp_thread(void *a, void *b, void *c)
{
	struct sockaddr_in sa = { .sin_family = AF_INET, .sin_port = htons(MB_TCP_PORT),
				.sin_addr.s_addr = htonl(INADDR_ANY) };
	int srv;

	srv = zsock_socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
	if (srv < 0) {
		mb_tcp_state = -errno;
		LOG_ERR("Modbus TCP: socket fallo, errno=%d", errno);
		return;
	}
	if (zsock_bind(srv, (struct sockaddr *)&sa, sizeof(sa)) < 0) {
		mb_tcp_state = -errno;
		LOG_ERR("Modbus TCP: bind fallo, errno=%d", errno);
		return;
	}
	if (zsock_listen(srv, 2) < 0) {
		mb_tcp_state = -errno;
		LOG_ERR("Modbus TCP: listen fallo, errno=%d", errno);
		return;
	}
	mb_tcp_state = 1;
	LOG_INF("Modbus TCP escuchando en el puerto %d (esclavo, unit id %u)",
		MB_TCP_PORT, cfg.unit_id);

	while (1) {
		mb_tcp_client = zsock_accept(srv, NULL, NULL);
		if (mb_tcp_client < 0) { k_msleep(200); continue; }
		mb_tcp_conns++;
		LOG_INF("Modbus TCP: maestro conectado");
		while (mb_tcp_txn(mb_tcp_client) >= 0) {
			/* siguiente transaccion */
		}
		LOG_INF("Modbus TCP: maestro desconectado");
		zsock_close(mb_tcp_client);
		mb_tcp_client = -1;
	}
}
K_THREAD_DEFINE(mb_tcp_tid, 4096, mb_tcp_thread, NULL, NULL, NULL, 7, 0, 3000);

static void enc_isr(const struct device *dev, struct gpio_callback *cb, uint32_t pins)
{
	uint8_t a = gpio_pin_get(gpa, ENC_A_PIN) ? 1 : 0;
	uint8_t b = gpio_pin_get(gpa, ENC_B_PIN) ? 1 : 0;
	uint8_t cur = (a << 1) | b;
	int8_t d = QTAB[(enc_prev << 2) | cur];

	enc_edges++;
	if (d == 2) {
		enc_errors++;          /* salto de 2 estados: rebote o pulso perdido */
	} else {
		enc_count += d;
	}
	enc_prev = cur;
}

static int enc_init(void)
{
	int ret;

	if (!device_is_ready(gpa)) {
		LOG_ERR("GPIOA no listo");
		return -ENODEV;
	}
	ret = gpio_pin_configure(gpa, ENC_A_PIN, GPIO_INPUT | GPIO_PULL_UP);
	if (ret) { LOG_ERR("PA0 configure: %d", ret); return ret; }
	ret = gpio_pin_configure(gpa, ENC_B_PIN, GPIO_INPUT | GPIO_PULL_UP);
	if (ret) { LOG_ERR("PA1 configure: %d", ret); return ret; }

	/* ambos flancos en A y B = decodificacion x4 */
	ret = gpio_pin_interrupt_configure(gpa, ENC_A_PIN, GPIO_INT_EDGE_BOTH);
	if (ret) { LOG_ERR("PA0 irq: %d", ret); return ret; }
	ret = gpio_pin_interrupt_configure(gpa, ENC_B_PIN, GPIO_INT_EDGE_BOTH);
	if (ret) { LOG_ERR("PA1 irq: %d", ret); return ret; }

	gpio_init_callback(&enc_cb, enc_isr, BIT(ENC_A_PIN) | BIT(ENC_B_PIN));
	gpio_add_callback(gpa, &enc_cb);

	enc_prev = (gpio_pin_get(gpa, ENC_A_PIN) ? 2 : 0) | (gpio_pin_get(gpa, ENC_B_PIN) ? 1 : 0);
	LOG_INF("Encoder listo: PA0/PA1 por software (x4), estado inicial AB=%u", enc_prev);
	return 0;
}

/* ---------------- trama SPE con la medida del encoder ---------------- */
#define ATT_ETHERTYPE 0x88B5      /* IEEE 802 local experimental 1 */
#define ATT_MAGIC     0x56415245  /* "VARE" */

struct att_frame {
	struct net_eth_hdr eth;
	uint32_t magic;
	uint32_t seq;
	int32_t  count;      /* posicion del encoder (x4) */
	uint32_t edges;      /* flancos totales */
	uint32_t errors;     /* transiciones ilegales */
	uint32_t uptime_ms;
} __packed;

static int tx_sock = -1;
static struct sockaddr_ll tx_dst;

static int spe_tx_init(struct net_if *iface)
{
	/* OJO: Zephyr SOLO acepta proto = 0 | ETH_P_ALL | ETH_P_ECAT | ETH_P_IEEE802154
	 * en SOCK_RAW (ver packet_is_supported() en sockets_packet.c). Un ethertype
	 * propio da EAFNOSUPPORT (errno 106). El proto del socket solo filtra el RX;
	 * el ethertype real (ATT_ETHERTYPE) va en la cabecera de la trama. */
	tx_sock = zsock_socket(AF_PACKET, SOCK_RAW, htons(ETH_P_ALL));
	if (tx_sock < 0) {
		LOG_ERR("SPE: socket(AF_PACKET,SOCK_RAW) FALLO: ret=%d errno=%d", tx_sock, errno);
		return -errno;
	}
	memset(&tx_dst, 0, sizeof(tx_dst));
	tx_dst.sll_family   = AF_PACKET;
	tx_dst.sll_protocol = htons(ETH_P_ALL);
	tx_dst.sll_ifindex  = net_if_get_by_iface(iface);

	/* No se hace bind: con AF_PACKET/SOCK_RAW la interfaz de salida la elige
	 * sll_ifindex en cada sendto(). Asi el firmware no depende de en que
	 * puerto SPE este el cable. */
	if (0) {
		LOG_ERR("SPE: bind(ifindex=%d) FALLO: errno=%d", tx_dst.sll_ifindex, errno);
		zsock_close(tx_sock); tx_sock = -1;
		return -errno;
	}
	LOG_INF("Socket SPE listo en ifindex %d, ethertype 0x%04x", tx_dst.sll_ifindex, ATT_ETHERTYPE);
	return 0;
}

static int spe_tx(struct net_if *iface, uint32_t seq)
{
	struct att_frame f;
	struct net_linkaddr *ll = net_if_get_link_addr(iface);

	memset(&f, 0, sizeof(f));
	memset(f.eth.dst.addr, 0xff, 6);              /* broadcast: no hace falta saber la MAC del otro */
	memcpy(f.eth.src.addr, ll->addr, 6);
	f.eth.type      = htons(ATT_ETHERTYPE);
	f.magic         = htonl(ATT_MAGIC);
	f.seq           = htonl(seq);
	f.count         = (int32_t)htonl((uint32_t)enc_count);
	f.edges         = htonl(enc_edges);
	f.errors        = htonl(enc_errors);
	f.uptime_ms     = htonl(k_uptime_get_32());

	tx_dst.sll_ifindex = net_if_get_by_iface(iface);
	return zsock_sendto(tx_sock, &f, sizeof(f), 0, (struct sockaddr *)&tx_dst, sizeof(tx_dst));
}

/* ---------------- main ---------------- */
int main(void)
{
	struct net_if *iface;
	uint32_t seq = 0, sent = 0, failed = 0;

	k_msleep(1500);
	LOG_INF("=== ATT Varec2500 — encoder -> SPE ===");
	LOG_INF("ADIN2111 ready=%d", device_is_ready(adin) ? 1 : 0);

	/* 1) sacar el bypass: mete el ADIN2111 en la linea SPE */
	if (!device_is_ready(gpd)) {
		LOG_ERR("GPIOD no listo: no puedo sacar el bypass");
	} else {
		int ret = gpio_pin_configure(gpd, BYPASS_EN_PIN, GPIO_OUTPUT_ACTIVE);
		LOG_INF("UC_BYPASS_EN (PD14) = 1 -> rele K1 energizado, ADIN2111 EN LINEA (ret=%d)", ret);
	}
	k_msleep(50);

	/* 2) encoder */
	enc_init();

	cfg_load();

	{
		int mb;
		static const enum uart_config_parity par[3] = {
			UART_CFG_PARITY_NONE, UART_CFG_PARITY_EVEN, UART_CFG_PARITY_ODD };

		/* Los parametros salen de la EEPROM, no van fijos en el firmware:
		 * en campo hay varios Varec en el mismo bus RS-485 y cada uno
		 * necesita su propia unit id. */
		mb_rtu.server.unit_id = cfg.unit_id;
		mb_rtu.serial.baud    = cfg.baud_div * 100u;
		mb_rtu.serial.parity  = par[cfg.parity % 3];
		mb_raw.server.unit_id = cfg.unit_id;

		/* RTU sobre el RS-485, solo si la configuracion lo pide */
		if (!(cfg.flags & CFG_F_RTU)) {
			LOG_INF("Modbus RTU: deshabilitado por configuracion");
		} else {
			mb = modbus_iface_get_by_name("modbus0");
			if (mb < 0 || modbus_init_server(mb, mb_rtu)) {
				LOG_ERR("Modbus RTU: no arranco (iface=%d)", mb);
			} else {
				LOG_INF("Modbus RTU listo: %u baud, paridad %u, esclavo unit id %u",
					cfg.baud_div * 100u, cfg.parity, cfg.unit_id);
			}
		}

		/* TCP sobre el SPE (RAW ADU). SIEMPRE activo: es el canal de
		 * configuracion. Si se pudiera apagar, un cambio equivocado dejaria
		 * el sensor inalcanzable. */
		mb_tcp_iface = modbus_iface_get_by_name("RAW_0");
		if (mb_tcp_iface < 0 || modbus_init_server(mb_tcp_iface, mb_raw)) {
			LOG_ERR("Modbus RAW/TCP: no arranco (iface=%d)", mb_tcp_iface);
		}
	}


	/* 3) levantar interfaces */
	iface = net_if_get_default();
	for (int i = 1; i <= 2; i++) {
		struct net_if *f = net_if_get_by_index(i);
		if (f) { net_if_up(f); }
	}

	/* 4) esperar carrier en la iface 1 (por donde transmitimos).
	 *    Se usa UNA SOLA iface a proposito: dos enlaces al mismo switch = lazo L2. */
	{
		net_mgmt_init_event_callback(&dhcp_cb, dhcp_handler, NET_EVENT_IPV4_ADDR_ADD);
		net_mgmt_add_event_callback(&dhcp_cb);
		for (int i = 1; i <= 2; i++) {
			struct net_if *f = net_if_get_by_index(i);
			if (f) { net_dhcpv4_start(f); }
		}
		LOG_INF("DHCP arrancado en los puertos SPE, esperando direccion...");
	}

	iface = net_if_get_by_index(1);
	for (int i = 0; i < 100 && !net_if_is_carrier_ok(iface); i++) {
		k_msleep(100);
	}
	LOG_INF("carrier al arrancar: if1=%d if2=%d",
		net_if_is_carrier_ok(net_if_get_by_index(1)) ? 1 : 0,
		net_if_is_carrier_ok(net_if_get_by_index(2)) ? 1 : 0);

	if (spe_tx_init(iface) < 0) {
		LOG_ERR("No se pudo abrir el socket SPE");
	}

	/* 5) transmitir la medida periodicamente */
	while (1) {
		k_msleep(cfg.push_ms10 * 10u);
		if (tx_sock < 0 && (seq % 8) == 0) {
			LOG_WRN("SPE: socket cerrado, reintentando...");
			spe_tx_init(iface);
		}
		{
			/* transmitir por la PRIMERA iface con portadora, sea cual sea */
			struct net_if *o = NULL;
			for (int i = 1; i <= 2; i++) {
				struct net_if *f = net_if_get_by_index(i);
				if (f && net_if_is_carrier_ok(f)) { o = f; break; }
			}
			if ((cfg.flags & CFG_F_PUSH) && tx_sock >= 0 && o != NULL) {
				int r = spe_tx(o, ++seq);
				if (r > 0) { sent++; } else { failed++; }
			}
		}
		if ((seq % 4) == 0) {
			LOG_INF("enc: count=%d edges=%u err=%u | SPE tx ok=%u fail=%u | carrier if1=%d if2=%d"
			" | MODBUS tcp_state=%d conns=%d iface=%d | cfg flags=0x%04x uid=%u",
				enc_count, enc_edges, enc_errors, sent, failed,
			net_if_is_carrier_ok(net_if_get_by_index(1)) ? 1 : 0,
			net_if_is_carrier_ok(net_if_get_by_index(2)) ? 1 : 0,
			mb_tcp_state, mb_tcp_conns, mb_tcp_iface, cfg.flags, cfg.unit_id);
		}
	}
	return 0;
}
