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
#include <zephyr/net/net_if.h>
#include <zephyr/net/ethernet.h>
#include <zephyr/net/socket.h>
#include <errno.h>
#include <zephyr/net/phy.h>
#include <zephyr/logging/log.h>
LOG_MODULE_REGISTER(att, LOG_LEVEL_INF);

static const struct device *const adin = DEVICE_DT_GET(DT_NODELABEL(adin2111));
static const struct device *const gpa  = DEVICE_DT_GET(DT_NODELABEL(gpioa));
static const struct device *const gpd  = DEVICE_DT_GET(DT_NODELABEL(gpiod));
static const struct device *const rs485 = DEVICE_DT_GET(DT_NODELABEL(usart2));

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
static char r485_line[96];
static int  r485_len;

static void rs485_poll(void)
{
	unsigned char c;

	while (uart_poll_in(rs485, &c) == 0) {
		r485_rx_bytes++;
		if (c == 10 || c == 13 || r485_len >= (int)sizeof(r485_line) - 1) {
			if (r485_len > 0) {
				r485_line[r485_len] = 0;
				LOG_INF("RS485 RX <<< \"%s\"  (total %u bytes)", r485_line, r485_rx_bytes);
				r485_len = 0;
			}
		} else {
			r485_line[r485_len++] = (char)c;
		}
	}
}

static void rs485_send(const char *msg)
{
	while (*msg) { uart_poll_out(rs485, *msg++); }
}

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
	tx_sock = zsock_socket(AF_PACKET, SOCK_RAW, htons(ATT_ETHERTYPE));
	if (tx_sock < 0) {
		LOG_ERR("socket AF_PACKET fallo: %d", errno);
		return -errno;
	}
	memset(&tx_dst, 0, sizeof(tx_dst));
	tx_dst.sll_family   = AF_PACKET;
	tx_dst.sll_protocol = htons(ATT_ETHERTYPE);
	tx_dst.sll_ifindex  = net_if_get_by_iface(iface);

	if (zsock_bind(tx_sock, (struct sockaddr *)&tx_dst, sizeof(tx_dst)) < 0) {
		LOG_ERR("bind fallo: %d", errno);
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

	if (!device_is_ready(rs485)) {
		LOG_ERR("RS485 (usart2) NO listo");
	} else {
		LOG_INF("RS485 listo: USART2 115200 8N1 (PA12=TX PA11=RX), DE automatico");
	}


	/* 3) levantar interfaces */
	iface = net_if_get_default();
	for (int i = 1; i <= 2; i++) {
		struct net_if *f = net_if_get_by_index(i);
		if (f) { net_if_up(f); }
	}

	/* 4) esperar carrier en la iface 1 (por donde transmitimos).
	 *    Se usa UNA SOLA iface a proposito: dos enlaces al mismo switch = lazo L2. */
	iface = net_if_get_by_index(1);
	for (int i = 0; i < 100 && !net_if_is_carrier_ok(iface); i++) {
		k_msleep(100);
	}
	LOG_INF("iface 1: carrier=%d", net_if_is_carrier_ok(iface) ? 1 : 0);

	if (spe_tx_init(iface) < 0) {
		LOG_ERR("No se pudo abrir el socket SPE");
	}

	/* 5) transmitir la medida periodicamente */
	while (1) {
		k_msleep(500);
		rs485_poll();
		if ((seq % 2) == 0) {
			char b[64];
			snprintk(b, sizeof(b), "ATT485 seq=%u enc=%d" "\r\n", seq, enc_count);
			rs485_send(b);
		}
		if (tx_sock >= 0 && net_if_is_carrier_ok(iface)) {
			int r = spe_tx(iface, ++seq);
			if (r > 0) { sent++; } else { failed++; }
		}
		if ((seq % 4) == 0) {
			LOG_INF("RS485 rx=%u bytes | encoder: count=%d edges=%u err=%u | SPE tx: ok=%u fail=%u carrier=%d", r485_rx_bytes,
				enc_count, enc_edges, enc_errors, sent, failed,
				net_if_is_carrier_ok(iface) ? 1 : 0);
		}
	}
	return 0;
}
