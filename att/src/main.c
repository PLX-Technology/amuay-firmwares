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
#include <zephyr/dfu/mcuboot.h>
#include <zephyr/drivers/sensor.h>
#include <zephyr/drivers/hwinfo.h>
#include <zephyr/net/ethernet_mgmt.h>
#include <zephyr/sys/crc.h>
#include <errno.h>
#include <zephyr/net/phy.h>
#include <zephyr/init.h>
#include <zephyr/logging/log.h>
LOG_MODULE_REGISTER(att, LOG_LEVEL_INF);

/* Build de BANCO: SPE/ADIN2111 desactivado (OA-SPI roto cuelga el arranque).
 * Se conserva TODO el codigo SPE; solo se salta en tiempo de compilacion. */
// #define BENCH_NO_SPE 1  (SPE activado para probar bit-bang 1MHz)
static const struct device *const adin = DEVICE_DT_GET(DT_NODELABEL(adin2111));
static const struct device *const gpa  = DEVICE_DT_GET(DT_NODELABEL(gpioa));
static const struct device *const gpd  = DEVICE_DT_GET(DT_NODELABEL(gpiod));
static const struct device *const attphy[2] = {
	DEVICE_DT_GET(DT_NODELABEL(attphy1)),
	DEVICE_DT_GET(DT_NODELABEL(attphy2)),
};

#define BYPASS_EN_PIN 14   /* PD14 = UC_BYPASS_EN */

/* --- K1 EN LINEA ANTES DE QUE ARRANQUE EL ADIN2111 -------------------
 * El driver del ADIN2111 se inicializa en POST_KERNEL/60
 * (CONFIG_ETH_INIT_PRIORITY), o sea ANTES de main(). Si K1 sigue sin
 * energizar en ese momento, el PHY esta FUERA del par, entrena contra nada
 * y ya no reengancha. Como la ATT se alimenta POR EL PROPIO PAR, arranca
 * siempre asi: el cable esta puesto pero el PHY no.
 *
 * Conmutar el rele mas tarde NO lo arregla: K1 PUENTEA el par en vez de
 * abrirlo, asi que el extremo contrario no se entera (probado y descartado,
 * ver el vigilante retirado en 21ab9b0). La solucion no es reenganchar
 * despues sino no llegar tarde.
 *
 * Prioridad 55: despues del GPIO (40) y del SPI (50), antes del Eth (60).
 */
static int att_bypass_relay_early(void)
{
	if (!device_is_ready(gpd)) {
		return -ENODEV;
	}
	gpio_pin_configure(gpd, BYPASS_EN_PIN, GPIO_OUTPUT_ACTIVE);
	k_busy_wait(20000);   /* 20 ms: cierre mecanico del rele */

	return 0;
}
SYS_INIT(att_bypass_relay_early, POST_KERNEL, 55);
#define ENC_A_PIN      0   /* PA0  = ENCODER AA */
#define ENC_B_PIN      7   /* PA7  = canal 3 = A out (era PA1, pin equivocado) */

/* ---------------- decodificador de cuadratura por software ---------------- */
static struct gpio_callback enc_cb;
static volatile int32_t  enc_count;      /* posicion (x4) */
static volatile uint32_t enc_edges;      /* flancos vistos: prueba de vida del cableado */
static volatile uint32_t enc_errors;     /* transiciones ilegales = pulso perdido/rebote */
static volatile uint32_t enc_ea;         /* flancos vistos en PA0 (canal 1 = B out) */
static volatile uint32_t enc_eb;         /* flancos vistos en PA1 (canal 3 = A out) */
static volatile uint8_t  enc_pa, enc_pb; /* ultimo nivel de cada pin */
static volatile uint8_t  enc_prev;       /* estado previo (A<<1|B) */

/* Tabla de cuadratura x4: indice = (prev<<2)|cur -> -1, 0, +1, o 2 = ilegal */
static const int8_t QTAB[16] = {
	 0, +1, -1,  2,
	-1,  0,  2, +1,
	+1,  2,  0, -1,
	 2, -1, +1,  0,
};

/* ---------------- LEDs de usuario: espejo de los canales A y B ---------------- */
/* led1 = canal A (A out = PA7 = ENC_B_PIN);  led2 = canal B (B out = PA0 = ENC_A_PIN) */
static const struct gpio_dt_spec led_a = GPIO_DT_SPEC_GET(DT_ALIAS(led1), gpios);
static const struct gpio_dt_spec led_b = GPIO_DT_SPEC_GET(DT_ALIAS(led2), gpios);

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
	uint16_t tank_id;    /* identidad del tanque: viaja en cada trama */
	int32_t  cal_cnt_a;  /* calibracion 2 puntos: cuenta y nivel(mm) en A */
	int32_t  cal_lvl_a;
	int32_t  cal_cnt_b;  /* ...y en B. nivel = interpolacion lineal A..B  */
	int32_t  cal_lvl_b;
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
	cfg.tank_id = 0;                      /* 0 = sin asignar */
	cfg.cal_cnt_a = cfg.cal_lvl_a = 0;    /* sin calibrar: nivel = cuenta cruda */
	cfg.cal_cnt_b = cfg.cal_lvl_b = 0;
}

/* nivel de calibracion en curso (staging para el comando de captura) */
static int32_t cal_level_in;

/* nivel calibrado en mm por interpolacion lineal entre los dos puntos.
 * Sin calibrar (dc==0) devuelve la cuenta cruda. La direccion se corrige
 * sola: si count baja cuando el nivel sube, la pendiente es negativa. */
static int att_calibrated(void)
{
	return (cfg.cal_cnt_b - cfg.cal_cnt_a) != 0;
}
static int32_t att_level_mm(void)
{
	int32_t dc = cfg.cal_cnt_b - cfg.cal_cnt_a;
	if (dc == 0) { return enc_count; }
	return cfg.cal_lvl_a +
	       (int32_t)((int64_t)(enc_count - cfg.cal_cnt_a) *
	                 (cfg.cal_lvl_b - cfg.cal_lvl_a) / dc);
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
	LOG_INF("Config leida de EEPROM: flags=0x%04x unit_id=%u tank_id=%u"
		" baud=%u parity=%u push=%ums",
		cfg.flags, cfg.unit_id, cfg.tank_id, cfg.baud_div * 100u,
		cfg.parity, cfg.push_ms10 * 10u);
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

/* definidas mas abajo, junto al bloque de ambiente */
static int16_t att_temp_c10(void);
static int16_t att_humi_rh10(void);

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
	case 7: *reg = (uint16_t)((uint32_t)att_level_mm() >> 16); break;   /* nivel mm hi */
	case 8: *reg = (uint16_t)((uint32_t)att_level_mm() & 0xFFFF); break; /* nivel mm lo */
	case 9: *reg = att_calibrated() ? 1 : 0; break;
	case 10: *reg = (uint16_t)att_temp_c10(); break;   /* 0.1 C, 0x8000 = sin dato */
	case 11: *reg = (uint16_t)att_humi_rh10(); break;  /* 0.1 %, 0x8000 = sin sensor */
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
 *   HR 5 : tank_id (identidad del tanque; viaja en cada trama)
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
	case 5: *reg = cfg.tank_id; break;
	case 9: *reg = 0; break;
	case 10: *reg = (uint16_t)((uint32_t)cal_level_in >> 16); break;
	case 11: *reg = (uint16_t)((uint32_t)cal_level_in & 0xFFFF); break;
	case 12: *reg = (uint16_t)((uint32_t)cfg.cal_cnt_a >> 16); break;
	case 13: *reg = (uint16_t)((uint32_t)cfg.cal_cnt_a & 0xFFFF); break;
	case 14: *reg = (uint16_t)((uint32_t)cfg.cal_lvl_a >> 16); break;
	case 15: *reg = (uint16_t)((uint32_t)cfg.cal_lvl_a & 0xFFFF); break;
	case 16: *reg = (uint16_t)((uint32_t)cfg.cal_cnt_b >> 16); break;
	case 17: *reg = (uint16_t)((uint32_t)cfg.cal_cnt_b & 0xFFFF); break;
	case 18: *reg = (uint16_t)((uint32_t)cfg.cal_lvl_b >> 16); break;
	case 19: *reg = (uint16_t)((uint32_t)cfg.cal_lvl_b & 0xFFFF); break;
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
	case 5:
		/* 0 = sin asignar. El rango util lo decide el sitio, no el firmware. */
		cfg.tank_id = reg;
		break;
	case 10: cal_level_in = (int32_t)(((uint32_t)reg << 16) |
	                                  ((uint32_t)cal_level_in & 0xFFFF)); break;
	case 11: cal_level_in = (int32_t)(((uint32_t)cal_level_in & 0xFFFF0000u) |
	                                  (uint32_t)reg); break;
	case 9:
		if (reg == 0xA5) { return cfg_save() < 0 ? -EIO : 0; }
		if (reg == 0x5A) { cfg_defaults(); return cfg_save() < 0 ? -EIO : 0; }
		/* capturar punto A/B: cuenta actual + nivel(mm) puesto en HR10-11.
		 * No guarda solo: revisar con FC03 y luego 0xA5 para persistir. */
		if (reg == 0xA1) { cfg.cal_cnt_a = enc_count; cfg.cal_lvl_a = cal_level_in; return 0; }
		if (reg == 0xB1) { cfg.cal_cnt_b = enc_count; cfg.cal_lvl_b = cal_level_in; return 0; }
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

/* Reporta cada iface por su NOMBRE de dispositivo. El indice no basta:
 * al habilitar IPv4 pueden aparecer interfaces y correrse la numeracion. */
static void hb_cb(struct net_if *iface, void *ud)
{
	int *n = ud;
	const struct device *d = net_if_get_device(iface);

	LOG_INF("  iface %d dev=%-8s carrier=%d up=%d", ++(*n),
		d ? d->name : "?",
		net_if_is_carrier_ok(iface) ? 1 : 0, net_if_is_up(iface) ? 1 : 0);
}

static void enc_isr(const struct device *dev, struct gpio_callback *cb, uint32_t pins)
{
	uint8_t a = gpio_pin_get(gpa, ENC_A_PIN) ? 1 : 0;
	uint8_t b = gpio_pin_get(gpa, ENC_B_PIN) ? 1 : 0;
	uint8_t cur = (a << 1) | b;
	gpio_pin_set_dt(&led_a, b);   /* canal A = PA7 (ENC_B_PIN) */
	gpio_pin_set_dt(&led_b, a);   /* canal B = PA0 (ENC_A_PIN) */
	if (a != enc_pa) { enc_ea++; enc_pa = a; }
	if (b != enc_pb) { enc_eb++; enc_pb = b; }
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

	/* LEDs espejo de canal: led1 = A (PA7), led2 = B (PA0) */
	if (gpio_is_ready_dt(&led_a)) {
		gpio_pin_configure_dt(&led_a, GPIO_OUTPUT_INACTIVE);
	}
	if (gpio_is_ready_dt(&led_b)) {
		gpio_pin_configure_dt(&led_b, GPIO_OUTPUT_INACTIVE);
	}
	gpio_pin_set_dt(&led_a, gpio_pin_get(gpa, ENC_B_PIN));
	gpio_pin_set_dt(&led_b, gpio_pin_get(gpa, ENC_A_PIN));
	LOG_INF("LEDs de canal listos: led1=canal A (PA7) led2=canal B (PA0)");
	return 0;
}

/* ---------------- ambiente: temperatura y humedad ---------------- */
/* Centinela de "dato no disponible". Se envia tal cual en la trama para que
 * el consumidor distinga "0 grados" de "no hay sensor". */
#define ATT_NA ((int16_t)0x8000)

static const struct device *const tsens = DEVICE_DT_GET_OR_NULL(DT_NODELABEL(adt75));

/* Temperatura ambiente en decimas de grado C (IC13 = ADT75, I2C1 0x48). */
static int16_t att_temp_c10(void)
{
	struct sensor_value v;

	if (tsens == NULL || !device_is_ready(tsens)) {
		return ATT_NA;
	}
	if (sensor_sample_fetch(tsens) < 0) {
		return ATT_NA;
	}
	if (sensor_channel_get(tsens, SENSOR_CHAN_AMBIENT_TEMP, &v) < 0) {
		return ATT_NA;
	}
	return (int16_t)(v.val1 * 10 + v.val2 / 100000);
}

/* Humedad relativa en decimas de %. LA ATT NO LLEVA SENSOR DE HUMEDAD: el
 * campo queda RESERVADO en el formato y se envia ATT_NA. Cuando exista uno
 * (el conector de expansion SV10 ya saca I2C1), solo hay que rellenarlo
 * aqui: la trama y el mapa Modbus NO cambian. */
static int16_t att_humi_rh10(void)
{
	return ATT_NA;
}

/* ---------------- MAC unica por placa ---------------- */
/* El devicetree da la MISMA MAC a todas las ATT. Con varias en la misma
 * red L2 el switch ve una direccion saltando entre puertos y las sesiones
 * unicast (DHCP, Modbus TCP, mcumgr) se vuelven poco fiables. Se sustituye
 * por una derivada del identificador unico del silicio.
 *
 * OJO: net_mgmt rechaza el cambio con -EACCES si la interfaz esta arriba,
 * asi que hay que bajarla antes; el main la levanta despues.
 */
static void att_set_unique_mac(void)
{
	uint8_t uid[16], mac[6];
	ssize_t n = hwinfo_get_device_id(uid, sizeof(uid));

	if (n < 8) {
		LOG_WRN("UID no disponible (%d): conservo la MAC del devicetree",
			(int)n);
		return;
	}

	/* Mezclar, no truncar: los bytes altos del UID son comunes a todo un
	 * lote de obleas, asi que quedarse con los primeros podria colisionar
	 * justo entre placas fabricadas juntas -- que es nuestro caso. */
	mac[0] = 0x02;   /* localmente administrada (bit 1), unicast (bit 0 = 0) */
	mac[1] = 0x00;
	for (int i = 0; i < 4; i++) {
		mac[2 + i] = uid[i] ^ uid[(i + n / 2) % n];
	}

	for (int i = 1; i <= 2; i++) {
		struct net_if *f = net_if_get_by_index(i);
		struct ethernet_req_params p;
		int r;

		if (f == NULL) {
			continue;
		}
		memset(&p, 0, sizeof(p));
		memcpy(p.mac_address.addr, mac, sizeof(mac));
		/* el puerto 2 va uno por encima, como en el devicetree */
		p.mac_address.addr[5] = (uint8_t)(mac[5] + (i - 1));

		net_if_down(f);   /* obligatorio: si no, -EACCES */
		r = net_mgmt(NET_REQUEST_ETHERNET_SET_MAC_ADDRESS, f, &p, sizeof(p));
		if (r < 0) {
			LOG_WRN("iface %d: no pude fijar la MAC (%d)", i, r);
		}
	}

	LOG_INF("MAC derivada del UID: %02x:%02x:%02x:%02x:%02x:%02x (+1 en el puerto 2)",
		mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
}

/* ---------------- trama SPE con la medida del encoder ---------------- */
#define ATT_ETHERTYPE 0x88B5      /* IEEE 802 local experimental 1 */
#define ATT_MAGIC     0x56415245  /* "VARE" */

struct att_frame {
	struct net_eth_hdr eth;
	uint32_t magic;
	uint16_t version;    /* 1. Sin esto, cambiar el formato rompe a los
	                      * consumidores en silencio. */
	uint16_t tank_id;    /* de la EEPROM: la placa lleva su identidad */
	uint32_t seq;
	int32_t  count;      /* posicion del encoder (x4) -- REFERENCIA cruda */
	int32_t  level_mm;   /* nivel calibrado (mm); == count si sin calibrar */
	uint32_t edges;      /* flancos totales */
	uint32_t errors;     /* transiciones ilegales */
	uint32_t uptime_ms;
	int16_t  temp_c10;   /* v3: temperatura ambiente en 0.1 C  (ATT_NA = sin dato) */
	int16_t  humi_rh10;  /* v3: humedad relativa en 0.1 %      (ATT_NA = sin sensor) */
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
	f.version       = htons(3);   /* v3: anade temp_c10 y humi_rh10 al final */
	f.tank_id       = htons(cfg.tank_id);
	f.seq           = htonl(seq);
	f.count         = (int32_t)htonl((uint32_t)enc_count);
	f.level_mm      = (int32_t)htonl((uint32_t)att_level_mm());
	f.edges         = htonl(enc_edges);
	f.errors        = htonl(enc_errors);
	f.uptime_ms     = htonl(k_uptime_get_32());
	f.temp_c10      = (int16_t)htons((uint16_t)att_temp_c10());
	f.humi_rh10     = (int16_t)htons((uint16_t)att_humi_rh10());

	tx_dst.sll_ifindex = net_if_get_by_iface(iface);
	return zsock_sendto(tx_sock, &f, sizeof(f), 0, (struct sockaddr *)&tx_dst, sizeof(tx_dst));
}

/* Vuelca los registros del PHY, los mismos que se leen en el MFS, para poder
 * comparar los dos extremos del enlace con el mismo criterio. */
static void att_phy_dump(void)
{
	for (int i = 0; i < 2; i++) {
		uint16_t id1 = 0, id2 = 0, b10l = 0, anst = 0, anctl = 0, pma = 0, adv = 0;
		int rc;

		if (!device_is_ready(attphy[i])) {
			LOG_WRN("PHY %d no listo", i + 1);
			continue;
		}
		rc = phy_read_c45(attphy[i], 1, 0x0002, &id1);
		phy_read_c45(attphy[i], 1, 0x0003, &id2);
		phy_read_c45(attphy[i], 1, 0x08F7, &b10l);
		phy_read_c45(attphy[i], 7, 0x0201, &anst);
		phy_read_c45(attphy[i], 7, 0x0200, &anctl);
		phy_read_c45(attphy[i], 1, 0x0834, &pma);
		phy_read_c45(attphy[i], 7, 0x0203, &adv);

		LOG_INF("PHY%d rc=%d ID=%04x:%04x B10L=%04x ANst=%04x ANctl=%04x(AN %s) PMA=%04x ADV=%04x",
			i + 1, rc, id1, id2, b10l, anst, anctl,
			(anctl & 0x1000) ? "ON" : "OFF", pma, adv);
	}
}

/* ---------------- main ---------------- */
/* --- OTA: confirmacion condicionada al enlace SPE ---------------------
 * Numero de envios SPE correctos SEGUIDOS que exigimos antes de confirmar
 * la imagen. Con push=500ms son ~2 s de enlace demostrado.
 */
#define OTA_CONFIRM_TX_STREAK 4

#ifdef CONFIG_MCUBOOT_IMG_MANAGER
static void ota_confirm_check(bool tx_ok)
{
	static uint8_t streak;
	static bool done;
	int r;

	if (done) {
		return;
	}
	if (!tx_ok) {
		streak = 0;   /* un fallo rompe la racha: hay que probarlo de nuevo */
		return;
	}
	if (++streak < OTA_CONFIRM_TX_STREAK) {
		return;
	}
	if (boot_is_img_confirmed()) {
		done = true;
		return;
	}
	r = boot_write_img_confirmed();
	LOG_INF("OTA: imagen CONFIRMADA tras %u envios SPE correctos (ret=%d)",
		streak, r);
	done = true;
}
#else
#define ota_confirm_check(ok) ((void)(ok))
#endif


int main(void)
{
	struct net_if *iface;
	uint32_t seq = 0, sent = 0, failed = 0;

	k_msleep(1500);
	LOG_INF("=== ATT Varec2500 — encoder -> SPE ===");
#ifdef CONFIG_MCUBOOT_IMG_MANAGER
	LOG_INF("OTA: imagen %s",
		boot_is_img_confirmed() ? "confirmada" :
		"A PRUEBA (revertira si el SPE no transmite)");
#endif
	LOG_INF("ADIN2111 ready=%d", device_is_ready(adin) ? 1 : 0);

	/* 1) sacar el bypass: mete el ADIN2111 en la linea SPE */
	if (!device_is_ready(gpd)) {
		LOG_ERR("GPIOD no listo: no puedo sacar el bypass");
	} else {
		int ret = gpio_pin_configure(gpd, BYPASS_EN_PIN, GPIO_OUTPUT_ACTIVE);
		LOG_INF("UC_BYPASS_EN (PD14) = 1 -> rele K1 energizado, ADIN2111 EN LINEA (ret=%d)", ret);
		/* Redundante: att_bypass_relay_early() ya lo cerro antes de que
		 * arrancara el PHY. Se conserva por la traza de log. */
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


#ifndef BENCH_NO_SPE
	/* 3) levantar interfaces. La MAC se fija ANTES: net_mgmt la rechaza
	 *    con -EACCES si la interfaz ya esta arriba, y el DHCP tiene que
	 *    salir con la definitiva o pedira concesion con la vieja. */
	att_set_unique_mac();

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
#else
	(void)iface;
	LOG_INF("BANCO: SPE deshabilitado -> solo encoder + RS-485 Modbus RTU");
#endif

	/* 5) transmitir la medida periodicamente */
	while (1) {
		k_msleep(cfg.push_ms10 * 10u);
#ifndef BENCH_NO_SPE
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
				/* solo confirmamos la imagen si el SPE transmite de verdad */
				ota_confirm_check(r > 0);
			}
		}
#else
		seq++;   /* sin SPE: avanzar seq para la cadencia del heartbeat */
#endif
		if ((seq % 4) == 0) {
			LOG_INF("enc: count=%d level=%dmm cal=%d | edges=%u err=%u PA0=%u PA7=%u | tx ok=%u fail=%u tank=%u",
				enc_count, att_level_mm(), att_calibrated(), enc_edges, enc_errors,
				enc_ea, enc_eb, sent, failed, cfg.tank_id);
#ifndef BENCH_NO_SPE
			{ struct hb { int n; } h = { 0 }; net_if_foreach(hb_cb, &h.n); }
			att_phy_dump();
#endif
		}
	}
	return 0;
}
