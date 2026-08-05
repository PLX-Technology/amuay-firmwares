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
 * Se puede desactivar con CFG_F_NO_K1 (HR 0 bit2) en las placas con SJ1
 * puenteado, donde el ADIN2111 ya esta en la linea por hardware.
 */
#include <zephyr/kernel.h>
#include <stm32_ll_gpio.h>
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
/* Nivel REAL de PD14 releido del pin (el IDR del STM32 refleja el nivel
 * fisico aunque el pin sea salida). 1 = la GPIO cumple. */
static int g_k1_level = -1;

/* NOTA: aqui hubo un SYS_INIT que cerraba K1 en POST_KERNEL/55, antes del
 * driver del PHY (ETH_INIT_PRIORITY=60). El orden era un defecto real, pero
 * NO era la causa de la falta de enlace: la pasarela dejo de recibir el 28 y
 * ese cambio es del 30. Se retira porque adelantarlo puede estorbar -- si la
 * bobina necesita un rail que aun no esta, el intento temprano falla y la
 * reescritura de main() ya no produce TRANSICION. Se vuelve a la secuencia
 * que enganchaba el 28: una sola energizacion, dentro de main(). */
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

/* ---------------- Alimentacion: senal de bateria en PB11 ----------------
 *
 * Con la placa en bateria hay que CALLAR: transmitir es lo mas caro que hace
 * esta placa. El push por SPE enciende el PHY y la linea; responder por
 * RS-485 activa el ADM2587E, que es aislado y arrastra su propio convertidor.
 *
 * ⚠️ CONSECUENCIA QUE HAY QUE TENER PRESENTE: en bateria la ATT deja de
 * responder por Modbus, asi que NO se le puede cambiar la configuracion hasta
 * que vuelva la alimentacion externa. Es inherente a lo pedido -- callar es
 * callar -- pero conviene saberlo antes de quedarse mirando por que no
 * contesta.
 *
 * ⚠️⚠️ Y LA GRANDE, DECIDIDA A PROPOSITO EL 2026-08-04:
 * UN TANQUE EN BATERIA ES INDISTINGUIBLE DE UN TANQUE AVERIADO desde la TPU.
 * Se evaluo mandar un latido lento (una trama cada 60 s, ~1 % del gasto) y se
 * DESCARTO a favor del maximo ahorro. Es una decision consciente, no un olvido:
 * si algun dia aparece un tanque mudo, mirar aqui antes de buscar averias.
 *
 * Duele el doble por un problema de HARDWARE que sigue abierto: con el cable
 * SPE puesto, la ATT en bateria NO se presenta como PD, asi que el PSE del
 * field switch sondea cada 30 s, no encuentra nada y NUNCA la vuelve a
 * alimentar. Verificado el 2026-08-04 sobre tank1: seis ciclos de reintento
 * sin exito, con el LTC4296 sano y Vin en rango. Es decir: un tanque que cae a
 * bateria no vuelve solo -- agota la bateria y hay que ir fisicamente.
 * Pendiente de Mayker.
 *
 * ✅ POLARIDAD VERIFICADA EN LA PLACA DE TANK1 (2026-08-04): PB11 = 0 con
 * alimentacion externa, 1 en bateria.
 *
 * ⚠️ La prueba concluyente NO fue el silencio -- eso lo da igual una placa
 * apagada, y de hecho el slot SPE dejo de entregar, asi que por SPE no habria
 * podido transmitir aunque quisiera. Lo que lo demuestra es que el TIEMPO DE
 * MARCHA que reporta la ATT siguio subiendo sin reiniciarse (283 s -> 514 s) y
 * que la cuenta del encoder cambio (1012 -> 1010) mientras estaba callada: la
 * placa estuvo VIVA, contando, y callada por decision del firmware.
 */
#define BAT_PORT   DT_NODELABEL(gpiob)
#define BAT_PIN    11
/* 1 = PB11 en ALTO significa "en bateria". Ver att_en_bateria(). */
#define BAT_ACTIVO_ALTO  1

/* Iface del Modbus RTU, a ambito de fichero para poder deshabilitarla
 * cuando la placa pasa a bateria. -1 = no arranco o esta apagada. */
static int g_mb_rtu = -1;

static const struct device *gpb;
static volatile int g_en_bateria;      /* ultimo estado leido, para la traza */

/* Espejo de CFG_F_ENC_LEDS. Cache en vez de mirar cfg.flags: lo consulta la
 * ISR del encoder en cada flanco. `volatile` porque lo escribe el hilo de
 * Modbus al cambiar la configuracion y lo lee la interrupcion. */
static volatile int g_enc_leds;

/* Espejo de CFG_F_BAT. Ver att_en_bateria(). */
static volatile int g_bat_activo;

/* ¿Esta la placa alimentada por bateria?
 *
 * Sin puerto listo se devuelve 0 (= alimentacion externa) A PROPOSITO: ante la
 * duda, la placa TRANSMITE. Un falso "en bateria" dejaria el tanque mudo y sin
 * forma de diagnosticarlo en remoto, que es mucho peor que gastar de mas. */
static int att_en_bateria(void)
{
	int v;

	/* Sin la bandera, la placa NUNCA se cree en bateria: transmite siempre.
	 * Ante la duda, hablar -- un falso "en bateria" deja el tanque mudo y sin
	 * forma de diagnosticarlo en remoto.
	 *
	 * Se lee de la copia en cache y no de cfg.flags porque esta funcion se
	 * define antes que la configuracion (mismo patron que g_enc_leds). */
	if (!g_bat_activo || gpb == NULL) {
		return 0;
	}
	v = gpio_pin_get_raw(gpb, BAT_PIN);
	if (v < 0) {
		return 0;
	}
	return BAT_ACTIVO_ALTO ? (v != 0) : (v == 0);
}

/* ---------------- LEDs del conector RS-485 (D9 y D10) ----------------
 *
 * Van CABLEADOS A LAS LINEAS TX/RX del USART2 (confirmado por Mayker), no a un
 * GPIO propio. Por eso estan encendidos siempre: la linea TX de un UART EN
 * REPOSO se queda en ALTO, asi que el LED luce aunque no se transmita nada. No
 * indican actividad, indican que el UART esta inicializado.
 *
 * ⚠️ SE SUELTAN A ENTRADA, NUNCA SE FUERZAN A BAJO.
 * El ADM2587E no tiene linea de habilitacion: la direccion se la conmuta un
 * comparador ADCMP600 vigilando la propia linea TX. Forzar TX a bajo para
 * apagar el LED puede hacer que el comparador lo lea como "esta transmitiendo"
 * y el transceptor se ponga a atacar el bus diferencial de forma permanente.
 * Con varios Varec en el mismo bus, eso no apaga un LED: DEJA MUDOS A TODOS LOS
 * DEMAS EQUIPOS. Dejar el pin en alta impedancia quita el ataque del micro sin
 * afirmar un nivel.
 *
 * ⚠️ SOLO SE HACE CUANDO EL PUERTO NO SE USA DE VERDAD: RS-485 deshabilitado
 * por configuracion, o placa en bateria (donde ya se apaga el Modbus). Con el
 * puerto habilitado la placa TIENE que seguir escuchando: es esclavo Modbus y
 * no puede saber si hay un maestro hasta que pregunta.
 *
 * ⚠️ ALCANCE REAL, y conviene no prometer de mas: solo el LED de TX depende del
 * micro. El de RX lo gobierna la salida RO del transceptor, que el firmware no
 * controla; si ese sigue encendido, es hardware.
 */
#define R485_TX_PIN 12
#define R485_RX_PIN 11

/* Suelta o devuelve los pines del USART2. IDA Y VUELTA, en caliente.
 *
 * ⚠️ La vuelta NO puede hacerse con pinctrl_apply_state(): su configuracion
 * (PINCTRL_DT_DEV_CONFIG_GET) no es accesible desde la aplicacion -- vive en la
 * unidad de compilacion del driver -- y CONFIG_PM_DEVICE no esta activado en
 * esta placa. Se reprograma el pin a mano con LL, con los MISMOS valores que
 * declara el devicetree, para que no haya dos verdades:
 *
 *   PA11 (RX)  AF3 + pull-up   <- att_wba65.dts, usart2_rx_pa11_pu
 *   PA12 (TX)  AF3 + pull-up   <- stm32wba65cgux-pinctrl.dtsi:784
 *
 * ⚠️ El pull-up de RX no es decorativo: sin el, con el bus RS-485 abierto la
 * entrada flota y el UART lee basura con errores de trama. Si se toca esto, se
 * toca tambien el devicetree. */
static int g_485_soltado;

/* Definida mas abajo, tras la struct del Modbus RTU: la usa el manejador
 * de escritura de HR 0, que va antes. */
static void att_485_puerto(int encender);

static void att_485_pines(int soltar)
{
	if (soltar == g_485_soltado) {
		return;
	}
	if (soltar) {
		/* Alta impedancia: el micro deja de atacar la linea. NO se pone a
		 * cero, por lo del comparador de direccion explicado arriba. */
		gpio_pin_configure(gpa, R485_TX_PIN, GPIO_INPUT);
		gpio_pin_configure(gpa, R485_RX_PIN, GPIO_INPUT);
		LOG_INF("RS-485: pines TX/RX en alta impedancia (LED de TX apagado;"
			" el de RX lo gobierna el transceptor)");
	} else {
		LL_GPIO_SetAFPin_8_15(GPIOA, LL_GPIO_PIN_11, LL_GPIO_AF_3);
		LL_GPIO_SetAFPin_8_15(GPIOA, LL_GPIO_PIN_12, LL_GPIO_AF_3);
		LL_GPIO_SetPinPull(GPIOA, LL_GPIO_PIN_11, LL_GPIO_PULL_UP);
		LL_GPIO_SetPinPull(GPIOA, LL_GPIO_PIN_12, LL_GPIO_PULL_UP);
		LL_GPIO_SetPinMode(GPIOA, LL_GPIO_PIN_11, LL_GPIO_MODE_ALTERNATE);
		LL_GPIO_SetPinMode(GPIOA, LL_GPIO_PIN_12, LL_GPIO_MODE_ALTERNATE);
		LOG_INF("RS-485: pines devueltos al USART2 (AF3)");
	}
	g_485_soltado = soltar;
}


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
/* NO energizar K1. Opt-in a proposito: el rele en reposo puentea P1<->P2 y
 * deja el ADIN2111 FUERA de la linea, asi que en una placa sin el puente SJ1
 * activar esto la deja sin enlace de red y sin forma de arreglarlo salvo por
 * consola serie o RS-485. El defecto (bit a 0) energiza, como siempre. */
#define CFG_F_NO_K1  BIT(2)
/* LEDs de canal del encoder = MODO DIAGNOSTICO, apagados por defecto.
 *
 * Parpadean con cada flanco del encoder, asi que en un tanque que se mueve
 * estan encendidos casi todo el tiempo, consumiendo sin que nadie los mire:
 * en campo no hay quien los vea. Se dejan encendibles desde la pasarela para
 * comprobar el cableado del encoder sin desplazarse hasta el tanque.
 *
 * ⚠️ Opt-IN, y por eso es bit y no un defecto invertido: si algun dia se
 * queda encendido por error, se apaga desde la pasarela; al reves habria que
 * ir al tanque. */
#define CFG_F_ENC_LEDS BIT(3)
/* ⚠️ SUSPENDER TRANSMISIONES EN BATERIA. OPT-IN, APAGADO POR DEFECTO.
 *
 * NO se activa de fabrica porque la senal PB11 NO SIGNIFICA LO MISMO EN TODAS
 * LAS PLACAS. Verificado el 2026-08-04: en la ATT de tank1 vale 0 con
 * alimentacion externa; en la de tank21, con alimentacion normal, vale 1 -- y
 * el firmware la dio por bateria y la dejo MUDA al arrancar, 13 minutos, hasta
 * que se miro la consola.
 *
 * Ese es el peor modo de fallo que puede tener esta placa: se dispara SOLO AL
 * ARRANCAR, deja el tanque sin reportar, y desde la pasarela es
 * indistinguible de un sensor averiado.
 *
 * Con 50 tanques, activarlo por defecto seria repartir esa loteria por toda la
 * planta. Se enciende POR UNIDAD, y solo despues de comprobar en esa placa
 * concreta que PB11 = 0 con alimentacion externa. */
#define CFG_F_BAT    BIT(4)

static const struct device *const eep = DEVICE_DT_GET(DT_NODELABEL(eeprom0));
static struct att_cfg cfg;

/* Aplica el estado de los LEDs de canal. Al apagarlos hay que FORZARLOS A CERO
 * aqui: si el ultimo flanco del encoder los dejo encendidos, sin esto se
 * quedarian asi para siempre -- que es justo lo que veniamos a arreglar.
 *
 * Se llama al arrancar y cada vez que la pasarela toca HR 0. */
static void enc_leds_aplicar(void)
{
	/* Aqui se refrescan TODAS las banderas cacheadas: esta funcion ya se
	 * llama al arrancar y en cada escritura de HR 0, que son justo los dos
	 * momentos en que cfg.flags puede cambiar. */
	g_bat_activo = (cfg.flags & CFG_F_BAT) != 0;
	g_enc_leds = (cfg.flags & CFG_F_ENC_LEDS) != 0;
	if (!g_enc_leds) {
		if (gpio_is_ready_dt(&led_a)) { gpio_pin_set_dt(&led_a, 0); }
		if (gpio_is_ready_dt(&led_b)) { gpio_pin_set_dt(&led_b, 0); }
	}
}

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
/* Centinela de "sin dato" para el nivel. Espejo del ATT_NA de 16 bits que ya
 * usan temperatura y humedad, pero para el campo de 32 bits. */
#define ATT_LEVEL_NA ((int32_t)0x80000000)

/* false = la cuenta arranco sin referencia (no habia checkpoint en EEPROM).
 * Se declara aqui, y no junto al resto de la persistencia, porque
 * att_level_mm() la necesita y esta antes en el fichero. */
static bool enc_ref_ok;

static int32_t att_level_mm(void)
{
	int32_t dc = cfg.cal_cnt_b - cfg.cal_cnt_a;
	/* Sin referencia la CUENTA no significa nada: interpolar sobre ella
	 * daria un nivel falso pero plausible. Mejor no dar dato. */
	if (!enc_ref_ok) { return ATT_LEVEL_NA; }

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

/* ============ PERSISTENCIA DE LA CUENTA DEL ENCODER (EEPROM) ============
 * enc_count vive en RAM y un arranque en frio lo pone a 0. Como la
 * calibracion se guarda como CUENTAS (cal_cnt_a/cal_cnt_b), perder la cuenta
 * no da un cero evidente: da un nivel FALSO PERO PLAUSIBLE. Con un encoder
 * incremental no hay forma de recuperarlo solo.
 *
 * Anillo de checkpoints detras de la configuracion. Ver el porque del
 * dimensionado en el comentario de ENC_STORE_MIN_MS.
 * ==================================================================== */
#define ENC_STORE_BASE   64                 /* la cfg ocupa 0..33 */
#define ENC_STORE_END    8192               /* AT24C64 */
#define ENC_STORE_SLOT   16
#define ENC_STORE_N      ((ENC_STORE_END - ENC_STORE_BASE) / ENC_STORE_SLOT)  /* 508 */

/* Ritmo de escritura. La AT24C64 aguanta ~1e6 ciclos por celda:
 *   508 ranuras x 1e6 = 5.08e8 escrituras;  a 1 cada 30 s = ~483 anos.
 * Y solo se escribe si la cuenta CAMBIO, asi que un tanque quieto no gasta. */
#define ENC_STORE_MIN_MS   30000
#define ENC_STORE_DELTA    64               /* fuerza checkpoint si se mueve mucho */

struct enc_slot {
	uint32_t seq;
	int32_t  count;
	uint32_t edges;
	uint16_t crc;
	uint16_t pad;
} __packed;

static uint32_t enc_store_seq;      /* seq del ultimo checkpoint escrito */
static int      enc_store_idx = -1; /* ranura del ultimo checkpoint */
static int32_t  enc_store_last;     /* cuenta del ultimo checkpoint */
/* declarada arriba, antes de att_level_mm() */

static uint16_t enc_slot_crc(const struct enc_slot *r)
{
	return crc16_ansi((const uint8_t *)r, sizeof(*r) - 2 * sizeof(uint16_t));
}

/* Busca el checkpoint valido con seq mayor y restaura la cuenta. */
static void enc_store_load(void)
{
	struct enc_slot r;
	uint32_t best_seq = 0;
	int best_i = -1;
	struct enc_slot best;

	if (!device_is_ready(eep)) {
		LOG_WRN("EEPROM no lista: la cuenta del encoder arranca SIN REFERENCIA");
		return;
	}

	for (int i = 0; i < ENC_STORE_N; i++) {
		if (eeprom_read(eep, ENC_STORE_BASE + i * ENC_STORE_SLOT, &r, sizeof(r)) < 0) {
			continue;
		}
		if (r.crc != enc_slot_crc(&r) || r.seq == 0 || r.seq == 0xFFFFFFFFu) {
			continue;   /* ranura virgen (0xFF) o escritura rota */
		}
		if (r.seq > best_seq) {
			best_seq = r.seq;
			best_i = i;
			best = r;
		}
	}

	if (best_i < 0) {
		LOG_WRN("★ SIN CHECKPOINT en EEPROM: la cuenta arranca en 0 y la "
			"calibracion NO es aplicable. El nivel reportado no es fiable "
			"hasta re-referenciar.");
		enc_ref_ok = false;
		return;
	}

	enc_count      = best.count;
	enc_edges      = best.edges;
	enc_store_seq  = best.seq;
	enc_store_idx  = best_i;
	enc_store_last = best.count;
	enc_ref_ok     = true;
	LOG_INF("Cuenta restaurada de EEPROM: count=%d edges=%u (ranura %d, seq %u)",
		best.count, best.edges, best_i, best.seq);
}

/* Escribe un checkpoint en la SIGUIENTE ranura (reparto de desgaste). */
static int enc_store_write(void)
{
	struct enc_slot r;
	int idx, ret;

	if (!device_is_ready(eep)) { return -ENODEV; }

	idx = (enc_store_idx + 1) % ENC_STORE_N;
	r.seq   = ++enc_store_seq;
	r.count = enc_count;
	r.edges = enc_edges;
	r.pad   = 0;
	r.crc   = enc_slot_crc(&r);

	ret = eeprom_write(eep, ENC_STORE_BASE + idx * ENC_STORE_SLOT, &r, sizeof(r));
	if (ret < 0) {
		enc_store_seq--;
		return ret;
	}
	enc_store_idx  = idx;
	enc_store_last = r.count;
	enc_ref_ok     = true;
	return 0;
}

/* Llamar desde el bucle de 1 s. Decide si toca checkpoint. */
static void enc_store_tick(void)
{
	static int64_t last_ms;
	int64_t now = k_uptime_get();
	int32_t c = enc_count;
	int32_t delta = c - enc_store_last;

	if (delta < 0) { delta = -delta; }
	if (delta == 0) { return; }                 /* quieto: no se gasta EEPROM */

	if (delta < ENC_STORE_DELTA && (now - last_ms) < ENC_STORE_MIN_MS) {
		return;
	}
	last_ms = now;
	(void)enc_store_write();
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
 *   HR 0 : flags (bit0 = push L2, bit1 = Modbus RTU, bit2 = NO energizar K1,
 *          bit4 = suspender transmisiones en bateria (OPT-IN: PB11 no vale lo
 *          mismo en todas las placas, comprobarlo antes de activarlo),
 *          bit3 = LEDs de canal del encoder, DIAGNOSTICO, apagados por
 *          defecto; surten efecto al instante, sin guardar ni reiniciar)
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
	case 0: {
		const uint16_t antes = cfg.flags;

		cfg.flags = reg & (CFG_F_PUSH | CFG_F_RTU | CFG_F_NO_K1 |
				   CFG_F_ENC_LEDS | CFG_F_BAT);

		/* El RS-485 se enciende y se apaga EN CALIENTE desde la pasarela.
		 * Antes solo se leia al arrancar, asi que apagarlo exigia un
		 * reinicio -- y en un tanque en campo eso no es una opcion.
		 *
		 * ⚠️ NO se toca si estamos en bateria: alli el puerto ya esta
		 * apagado a proposito y encenderlo aqui se saltaria ese ahorro. */
		if (((antes ^ cfg.flags) & CFG_F_RTU) && !att_en_bateria()) {
			att_485_puerto((cfg.flags & CFG_F_RTU) != 0);
		}
		/* Los LEDs de diagnostico responden AL INSTANTE, sin esperar a
		 * guardar en EEPROM ni a un reinicio: se encienden para mirar el
		 * encoder mientras alguien esta delante del tanque. */
		enc_leds_aplicar();
		break;
	}
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

/* Enciende o apaga el puerto RS-485 completo: interfaz Modbus + pines.
 *
 * ⚠️ EL ORDEN IMPORTA en los dos sentidos. Al encender, los pines PRIMERO: un
 * servidor Modbus escuchando sobre pines en alta impedancia no llega al
 * transceptor. Al apagar, la interfaz PRIMERO: soltar los pines de un UART que
 * aun puede estar transmitiendo cortaria una trama por la mitad. */
static void att_485_puerto(int encender)
{
	if (encender) {
		att_485_pines(0);
		if (g_mb_rtu >= 0 && modbus_init_server(g_mb_rtu, mb_rtu)) {
			LOG_ERR("RS-485: no pude reactivar el servidor Modbus");
		}
	} else {
		if (g_mb_rtu >= 0) {
			modbus_disable((uint8_t)g_mb_rtu);
		}
		att_485_pines(1);
	}
}

/* ---- TCP sobre SPE: Zephyr no trae servidor, se usa RAW ADU ---- */
static int mb_tcp_iface = -1;
static volatile int mb_tcp_state;   /* 0=sin arrancar 1=escuchando -N=errno */
static volatile int mb_tcp_conns;
static volatile unsigned mb_tcp_zombis;  /* conexiones cerradas por silencio */
static int mb_tcp_client = -1;
/* Segundos sin recibir nada antes de dar por muerta una conexion. Un SCADA
 * sondea cada pocos segundos; 30 s no molesta a nadie legitimo y acota lo
 * que puede bloquear un cliente zombi. */
#define MB_TCP_IDLE_S 30

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
	if (rc <= 0) {
		/* EAGAIN = venció el tiempo de espera: cliente callado. Se cierra
		 * y se vuelve a accept(); NO es un error del que quejarse. */
		if (rc < 0 && errno == EAGAIN) {
			mb_tcp_zombis++;
			LOG_INF("Modbus TCP: cliente callado %d s, se cierra", MB_TCP_IDLE_S);
		}
		return rc == 0 ? -ENOTCONN : -errno;
	}

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
		struct zsock_timeval tv = { .tv_sec = MB_TCP_IDLE_S, .tv_usec = 0 };

		mb_tcp_client = zsock_accept(srv, NULL, NULL);
		if (mb_tcp_client < 0) { k_msleep(200); continue; }
		mb_tcp_conns++;

		/* ⚠️ ESTO ES LO QUE EVITA QUE UN CLIENTE MUERTO MATE EL SERVIDOR.
		 *
		 * Este servidor atiende UN cliente cada vez. Sin tiempo de espera,
		 * zsock_recv(MSG_WAITALL) se queda bloqueado PARA SIEMPRE si el
		 * maestro se va sin cerrar (cable fuera, proceso matado, red que
		 * se corta). El hilo no vuelve nunca a accept(), las conexiones
		 * siguientes se apilan y al agotarse los contextos de red la pila
		 * IP entera deja de responder.
		 *
		 * Verificado en tank1 el 2026-08-04, DOS VECES: tras unas pocas
		 * conexiones la ATT dejaba de aceptar Modbus y de responder a ping,
		 * seguia enviando por SPE -- asi que el tanque parecia sano -- y NO
		 * se recuperaba sola: hacia falta ir a reiniciarla.
		 *
		 * Con el tiempo de espera, una conexion zombi cuesta como mucho
		 * MB_TCP_IDLE_S y el servidor sigue vivo. */
		if (zsock_setsockopt(mb_tcp_client, SOL_SOCKET, SO_RCVTIMEO,
				     &tv, sizeof(tv)) < 0) {
			LOG_WRN("Modbus TCP: no pude poner el tiempo de espera (errno=%d)",
				errno);
		}

		LOG_INF("Modbus TCP: maestro conectado (%u en total)", mb_tcp_conns);
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

	/* LEDs de canal solo en modo diagnostico (CFG_F_ENC_LEDS). Apagados por
	 * defecto: parpadean con CADA flanco, asi que en un tanque en movimiento
	 * estarian encendidos casi siempre y en campo no hay quien los mire.
	 *
	 * La condicion va DENTRO de la ISR y no vale con no configurar el pin:
	 * esta rutina corre en cada flanco del encoder y es donde se enciende. */
	if (g_enc_leds) {
		gpio_pin_set_dt(&led_a, b);   /* canal A = PA7 (ENC_B_PIN) */
		gpio_pin_set_dt(&led_b, a);   /* canal B = PA0 (ENC_A_PIN) */
	}
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

	/* LEDs espejo de canal: led1 = A (PA7), led2 = B (PA0).
	 * Se configuran igual, pero ARRANCAN APAGADOS y solo siguen al encoder
	 * si CFG_F_ENC_LEDS esta activo (modo diagnostico desde la pasarela).
	 * Antes se encendian aqui con el estado inicial del encoder. */
	if (gpio_is_ready_dt(&led_a)) {
		gpio_pin_configure_dt(&led_a, GPIO_OUTPUT_INACTIVE);
	}
	if (gpio_is_ready_dt(&led_b)) {
		gpio_pin_configure_dt(&led_b, GPIO_OUTPUT_INACTIVE);
	}
	enc_leds_aplicar();
	if (g_enc_leds) {
		gpio_pin_set_dt(&led_a, gpio_pin_get(gpa, ENC_B_PIN));
		gpio_pin_set_dt(&led_b, gpio_pin_get(gpa, ENC_A_PIN));
	}
	LOG_INF("LEDs de canal: %s (HR 0 bit3 = diagnostico)",
		g_enc_leds ? "ENCENDIDOS" : "apagados");
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
	 * el ethertype real (ATT_ETHERTYPE) va en la cabecera de la trama.
	 *
	 * ★ PROTO = 0, NO ETH_P_ALL. ESTA ERA LA FUGA DE BUFERES (2026-08-04).
	 *
	 * ETH_P_ALL significa literalmente "entregame TODAS las tramas de la red"
	 * (sockets_packet.c:94). Este socket es SOLO PARA TRANSMITIR -- nadie lee
	 * nunca de el -- pero la pila le clonaba y encolaba cada trama que pasaba
	 * por el segmento, y esos buferes no volvian jamas al pool.
	 *
	 * El sintoma: la placa se quedaba SORDA -- ni Modbus, ni ARP, ni ping --
	 * mientras SEGUIA TRANSMITIENDO por SPE (otro pool), asi que desde la
	 * pasarela parecia sana. No se recuperaba sola.
	 *
	 * Y explica por que empeoraba con el trafico: el segmento SPE esta
	 * PUENTEADO con la red de oficina (ver tpu/red-spe/README.md), o sea
	 * difusion constante. Tambien por que en su dia "se arreglo" aislando la
	 * red: se le quito trafico, pero el socket seguia tragandoselo todo.
	 *
	 * Con proto = 0 el socket no casa con ninguna trama entrante: no hay
	 * clonado, no hay encolado y no hay fuga. La transmision no se ve afectada
	 * -- el proto solo filtra el RX. */
	tx_sock = zsock_socket(AF_PACKET, SOCK_RAW, 0);
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

	/* La configuracion se lee ANTES del bypass porque la decision depende de
	 * ella. Esto retrasa unos ms la energizacion del rele, que es el lado
	 * seguro: la nota de arriba avisa de que intentarlo DEMASIADO PRONTO
	 * falla si la bobina necesita un rail que aun no esta. */
	cfg_load();

	/* Senal de bateria (PB11). Entrada pura, sin pull: el nivel lo impone el
	 * hardware de alimentacion. Si el puerto no esta listo, att_en_bateria()
	 * devuelve 0 y la placa transmite -- ante la duda, hablar. */
	gpb = DEVICE_DT_GET(DT_NODELABEL(gpiob));
	if (device_is_ready(gpb)) {
		int ret = gpio_pin_configure(gpb, BAT_PIN, GPIO_INPUT);

		g_en_bateria = att_en_bateria();
		LOG_INF("Alimentacion: PB11 = %d, funcion %s -> %s (ret=%d)",
			gpio_pin_get_raw(gpb, BAT_PIN),
			(cfg.flags & CFG_F_BAT) ? "ACTIVA" : "desactivada (HR 0 bit4)",
			g_en_bateria ? "BATERIA (se suspenden transmisiones)" : "transmite",
			ret);
	} else {
		gpb = NULL;
		LOG_ERR("GPIOB no listo: no puedo leer la senal de bateria,"
			" se asume alimentacion externa");
	}

	/* 1) sacar el bypass: mete el ADIN2111 en la linea SPE */
	if (cfg.flags & CFG_F_NO_K1) {
		/* ⚠️ Placa con SJ1 PUENTEADO: el ADIN2111 ya esta cableado en la
		 * linea y el rele no pinta nada -- ademas es de 5 V en un rail de
		 * 3,3 V, asi que ni cierra. Se deja el pin en BAJO de forma
		 * explicita, no sin configurar: un pin flotando en la puerta de un
		 * driver de bobina no es un estado, es una loteria. */
		if (device_is_ready(gpd)) {
			gpio_pin_configure(gpd, BYPASS_EN_PIN, GPIO_OUTPUT_INACTIVE);
			g_k1_level = gpio_pin_get_raw(gpd, BYPASS_EN_PIN);
		}
		LOG_INF("UC_BYPASS_EN: K1 NO se energiza (CFG_F_NO_K1). Se asume SJ1"
			" puenteado; PD14 = %d", g_k1_level);
	} else if (!device_is_ready(gpd)) {
		LOG_ERR("GPIOD no listo: no puedo sacar el bypass");
	} else {
		int ret = gpio_pin_configure(gpd, BYPASS_EN_PIN, GPIO_OUTPUT_ACTIVE);
		LOG_INF("UC_BYPASS_EN (PD14) = 1 -> rele K1 energizado, ADIN2111 EN LINEA (ret=%d)", ret);
		k_msleep(50);   /* margen para el cierre mecanico del rele */
		g_k1_level = gpio_pin_get_raw(gpd, BYPASS_EN_PIN);
		LOG_INF("PD14 releido del pin = %d  (%s)", g_k1_level,
			g_k1_level == 1 ? "la GPIO cumple: mirar aguas abajo"
					: "el pin NO sube: algo tira de esa red");
		/* Redundante: att_bypass_relay_early() ya lo cerro antes de que
		 * arrancara el PHY. Se conserva por la traza de log. */
	}
	k_msleep(50);

	/* 2) encoder */
	enc_init();

	enc_store_load();

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
			att_485_pines(1);
		} else {
			mb = modbus_iface_get_by_name("modbus0");
			if (mb < 0 || modbus_init_server(mb, mb_rtu)) {
				LOG_ERR("Modbus RTU: no arranco (iface=%d)", mb);
			} else {
				g_mb_rtu = mb;
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
		int bat;

		k_msleep(cfg.push_ms10 * 10u);

		/* ⚠️ EN BATERIA SE CALLA. Transmitir es de largo lo mas caro que hace
		 * esta placa: el push enciende el PHY y la linea SPE.
		 *
		 * El encoder SIGUE CONTANDO -- la ISR no se toca -- asi que al volver
		 * la alimentacion externa el nivel es correcto y no hay que recalibrar.
		 * Ese era el requisito implicito: ahorrar energia sin perder la medida.
		 *
		 * Se relee en cada vuelta, no una vez al arrancar: la placa puede
		 * pasar a bateria en caliente y hay que enterarse. */
		bat = att_en_bateria();
		if (bat != g_en_bateria) {
			g_en_bateria = bat;
			LOG_INF("Alimentacion: paso a %s",
				bat ? "BATERIA -> transmisiones suspendidas"
				    : "externa -> transmisiones reanudadas");

			/* El RS-485 hay que callarlo APAGANDO LA INTERFAZ; no basta con
			 * no llamar a nada, porque la ATT es ESCLAVO Modbus y responde
			 * sola en cuanto un maestro pregunta. Cada respuesta activa el
			 * ADM2587E, que es aislado y arrastra su propio convertidor.
			 *
			 * ⚠️ Solo se reactiva si la configuracion lo pedia (CFG_F_RTU):
			 * volver de bateria no debe encender un RS-485 que estaba
			 * deshabilitado a proposito. */
			if (bat) {
				/* Puerto entero abajo: interfaz Modbus + pines. Apaga
				 * ademas el LED de TX, que en bateria es consumo puro. */
				att_485_puerto(0);
				LOG_INF("RS-485 apagado por bateria");
			} else if (cfg.flags & CFG_F_RTU) {
				att_485_puerto(1);
				LOG_INF("RS-485 reactivado");
			}
		}
		if (bat) {
			continue;
		}
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
			enc_store_tick();
			{ struct hb { int n; } h = { 0 }; net_if_foreach(hb_cb, &h.n); }
			LOG_INF("PD14 = %d", gpio_pin_get_raw(gpd, BYPASS_EN_PIN));
			att_phy_dump();
#endif
		}
	}
	return 0;
}
