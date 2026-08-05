/*
 * Copyright (c) 2024 Analog Devices Inc.
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include <zephyr/logging/log.h>
LOG_MODULE_REGISTER(eth_adin6310, CONFIG_LOG_DEFAULT_LEVEL);

#include <zephyr/kernel.h>
#include <stdio.h>
#include <zephyr/device.h>
#include <zephyr/net/net_config.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/drivers/spi.h>
#include <zephyr/drivers/sensor/ltc4296.h>
#include <zephyr/sys/byteorder.h>
#include <mxc_sys.h>
#include <flc_regs.h>                   /* actrl: leer el USN del info block */

#include "SMP_stack_api.h"
#include "SES_port_api.h"
#include "SES_switch.h"
#include "SES_vlan.h"
#include "SES_frame_api.h"
#include "SES_codes.h"
#include "SES_interface_management.h"
#include "TSN_ptp.h"
#include "zephyr/sys/util.h"

#define ADIN6310_SPI_WR_HEADER	0x40
#define ADIN6310_SPI_RD_HEADER	0x80

static struct k_sem semaphores[50];

/* Lecturas fallidas acumuladas del enlace con el ADIN6310. Visible por SWD:
 * si crece, hay un problema en el enlace SPI que antes quedaba oculto
 * porque el hilo lector moria en silencio al primero. */
volatile unsigned g_rd_err_n;
K_THREAD_STACK_DEFINE(stack_area, 2000);
K_SEM_DEFINE(reader_thread_sem, 0, 1);
K_MUTEX_DEFINE(spi_mutex);
volatile int g_link[6];
/* Barrido de PHY de los 6 puertos. Indice = numero de macPort.
 * El mapeo conocido: macPort5 = slot Port 4 (rotulado en la placa). */
volatile unsigned short g_ph_id1[6];    /* 0x010002  0x0283 = ADIN1100 visto */
volatile unsigned short g_ph_id2[6];    /* 0x010003  0xBC81 */
volatile unsigned short g_ph_b10l[6];   /* 0x0108F7  B10L link status */
volatile unsigned short g_ph_anst[6];   /* 0x070201  AN status */
volatile unsigned short g_ph_anctl[6];  /* 0x070200  AN control, bit12 = AN on */
volatile unsigned short g_ph_pma[6];    /* 0x010834  PMA ctrl (maestro/esclavo) */
volatile int            g_ph_rc[6];     /* rc de la 1a lectura de cada puerto */
volatile unsigned int g_st[10];
volatile unsigned int g_st3[5];
volatile unsigned int g_rx[4], g_tx[4];
volatile unsigned short g_rj[16];
volatile unsigned short g_p4[16];
volatile unsigned short g_map[128];
volatile unsigned short g_an[4][6];
volatile unsigned short g_r[6];
volatile unsigned short g_v1, g_v2;
volatile unsigned short g_t1, g_t2;
volatile unsigned short g_ledw[4];
volatile int g_ledfunc;
volatile unsigned short g_ledrb;
volatile unsigned short g_phyid1[4], g_phyid2[4], g_ledctrl[4], g_ethid1, g_ethid2;
volatile int g_phyret[4];
volatile unsigned short g_probe[8];
volatile unsigned short g_scan[64];

void* SES_PORT_CreateSemaphore(int initCount, int maxCount)
{
	for (int i = 0; i < 50; i++) {
		if (semaphores[i].limit == 0) {
			k_sem_init(&semaphores[i], initCount, maxCount);

			return &semaphores[i];
		}
	}

	return 0;
}


int32_t SES_PORT_WaitSemaphore(int* semaphore_p, int timeout)
{
	struct k_sem *sem = (struct k_sem *)semaphore_p;
	int ret;

	ret = k_sem_take(sem, K_MSEC(timeout));
	if (ret == -ETIMEDOUT)
		return SES_PORT_TIMEOUT;

	return SES_PORT_OK;
}

int32_t SES_PORT_SignalSemaphore(int* semaphore_p)
{
	struct k_sem *sem = (struct k_sem *)semaphore_p;

	if (sem == NULL)
		return SES_PORT_INVALID_PARAM;

	k_sem_give(sem);

	return 1;
}

int32_t SES_PORT_DeleteSemaphore(int* semaphore_p)
{
	struct k_sem *sem = (struct k_sem *)semaphore_p;

	memset(sem, 0x0, sizeof(*sem));

	return 1;
}

int32_t SES_PORT_Malloc(void **buf_pp, int size)
{
	if (!buf_pp)
		return SES_PORT_BUF_ERR;
	
	*buf_pp = k_malloc(size);

	return 0;
}

int32_t SES_PORT_Free(void *buf_p)
{
	if (!buf_p)
		return SES_PORT_BUF_ERR;

	k_free(buf_p);

	return 0;
}

int SES_PORT_SPI_Init(void *param_p,
                      SES_PORT_intfType_t *intfType_p,
                      uint8_t *srcMac_p)
{
	*intfType_p = SES_PORT_spiInterface;

	return 0;
}

int SES_PORT_SPI_Release(int intfHandle)
{
	return 0;
}

static int adin6310_spi_write(int intfHandle, int size, void *data_p)
{
	const struct spi_dt_spec dev_spi = SPI_DT_SPEC_GET(DT_NODELABEL(adin6310),
							   SPI_WORD_SET(8) |
							   SPI_OP_MODE_MASTER |
							   SPI_TRANSFER_MSB |
							   SPI_MODE_GET(0), 0);
	struct spi_buf tx_buf;
	struct spi_buf_set tx;
	uint8_t *txb;
	int ret = 0;

	k_mutex_lock(&spi_mutex, K_FOREVER);
	tx_buf.len = size + 1;
	txb = k_calloc(size + 1, 1);
	if (!txb) {
		LOG_ERR("Calloc error %d txb", (uint32_t)txb);
		ret = -ENOMEM;
		goto free_txb;
	}

	tx_buf.buf = txb;
	tx.buffers = &tx_buf;
	tx.count = 1;

	txb[0] = ADIN6310_SPI_WR_HEADER;
	memcpy(&txb[1], data_p, size);
	ret = spi_write_dt(&dev_spi, &tx);
	
	SES_PORT_Free(data_p);
free_txb:
	k_free(txb);
	k_mutex_unlock(&spi_mutex);

	return ret;
}

int adin6310_spi_read(uint8_t *buf, uint32_t len)
{
	int ret = 0;
	uint8_t *xfer_buf_tx;
	uint8_t *xfer_buf_rx;
	struct spi_buf rx_buf;
	struct spi_buf tx_buf;
	struct spi_buf_set rx_buf_set;
	struct spi_buf_set tx_buf_set;
	const struct spi_dt_spec dev_spi = SPI_DT_SPEC_GET(DT_NODELABEL(adin6310),
							   SPI_WORD_SET(8) |
							   SPI_OP_MODE_MASTER |
							   SPI_TRANSFER_MSB |
							   SPI_MODE_GET(0), 0);

	k_mutex_lock(&spi_mutex, K_FOREVER);
	xfer_buf_rx = k_calloc(len + 1, sizeof(xfer_buf_rx));
	if (!xfer_buf_rx) {
		ret = -ENOMEM;
		goto unlock;
	}

	xfer_buf_tx = k_calloc(len + 1, sizeof(*xfer_buf_tx));
	if (!xfer_buf_tx) {
		ret = -ENOMEM;
		goto free_rx;
	}

	xfer_buf_tx[0] = ADIN6310_SPI_RD_HEADER;

	rx_buf.len = len + 1;
	rx_buf.buf = xfer_buf_rx;
	tx_buf.len = len + 1;
	tx_buf.buf = xfer_buf_tx;

	rx_buf_set.buffers = &rx_buf;
	rx_buf_set.count = 1;
	tx_buf_set.buffers = &tx_buf;
	tx_buf_set.count = 1;

	ret = spi_transceive_dt(&dev_spi, &tx_buf_set, &rx_buf_set);
	if (ret)
		goto free_tx;

	memcpy(buf, &xfer_buf_rx[1], len);

free_tx:
	k_free(xfer_buf_tx);
free_rx:
	k_free(xfer_buf_rx);
unlock:
	k_mutex_unlock(&spi_mutex);

	return ret;
}

int adin6310_read_message(int tbl_index)
{
	uint32_t frame_type;
	uint8_t *frame_buf;
	uint32_t padded_len;
	uint32_t rx_len;
	uint8_t header[4];
	int ret;

	ret = adin6310_spi_read(header, 4);
	if (ret)
		return ret;

	rx_len = ((header[1] & 0xF) << 8) | header[0];
	frame_type = (header[1] & 0xF0) >> 4;

	padded_len = rx_len + 0x3;
	padded_len &= ~GENMASK(1, 0);

	frame_buf = k_calloc(padded_len, sizeof(*frame_buf));
	if (!frame_buf)
		return -ENOMEM;

	ret = adin6310_spi_read(frame_buf, padded_len);
	if (ret)
		return ret;

	ret = SES_ReceiveMessage(tbl_index,
			SES_PORT_spiInterface,
			rx_len,
			(void*)frame_buf,
			(-1),
			(0),
			NULL);

	k_free(frame_buf);

	return ret;
}

void adin6310_msg_recv(void *p1, void *p2, void *p3)
{
	int ret;

	printf("Reader thread start\n");

	while (1) {
		k_sem_take(&reader_thread_sem, K_FOREVER);
		ret = adin6310_read_message(0);
		/* El hilo NO se suicida: antes un `return` aqui mataba la
		 * comunicacion con el switch para siempre ante un unico error
		 * de lectura. El semaforo bloquea, asi que no hay bucle
		 * caliente; se cuenta el fallo y se sigue. */
		if (ret)
			g_rd_err_n++;
	};
}

void adin6310_int_rdy()
{
	k_sem_give(&reader_thread_sem);
}



int adin6310_vlan_example()
{
	int32_t ret;

	for (uint16_t vid = 1; vid < 11; vid++) {
		ret = SES_SetVlanMode(vid, 0xFFF);
		printf("VID %d enabled on ports 0 to 5 :: %d\n", vid, ret);
		if (ret != SES_OK)
			return ret;
	}

	return 0;
}

int32_t SES_FirmwareCheck(void) {
	int32_t rv = -1;
	
	SES_appInfo_t firmwareInfo;
	rv = SES_GetFirmwareInfo(&firmwareInfo, sizeof(firmwareInfo));
	if (rv == SES_PORT_OK)
	printf("Check Firmware Version :: %s-%s-%d\n", firmwareInfo.partNum, firmwareInfo.version, firmwareInfo.buildNumber);
	else
	printf("Check Firmware Version :: Error\n");
	
	return rv;
	}


int32_t timesync(uint8_t mac_addr[6]) {
	int32_t rv = 0;
 
	if (SES_OK != SES_PtpStart())
		return;
	//Configure CMLDS, Clock identity
	const TSN_ptp_init_cmlds_ds_t initDs = { {mac_addr[0], mac_addr[1], mac_addr[2],
						mac_addr[3], mac_addr[4], mac_addr[5], 0xff, 0xff } };
	printf("SES_PtpInitCmlds :: %d\n", SES_PtpInitCmlds(&initDs));
 
 
	uint16_t numberPtpPorts = 6;
	uint16_t linkPortNumber[6] = { 1, 2, 3, 4,5, 6 };

	TSN_ptp_init_instance_ds_t init_s = {
		.clock_identity = { mac_addr[0], mac_addr[1], mac_addr[2], mac_addr[3], mac_addr[4], mac_addr[5], 0x00, 0x00 },
		.clock_number = 0,
		.domain_number = 0
	};
	uint32_t instanceIndex;
	rv = SES_PtpCreatePtpInstance(TSN_ptp_instance_type_ptp_relay, numberPtpPorts, &linkPortNumber, &init_s, &instanceIndex);
 
	// Initialize default_ds
	TSN_ptp_default_ds_t default_ds;
	rv = SES_PtpGetDefaultDs(instanceIndex, &default_ds);
	default_ds.instance_enable = 1;
	rv = SES_PtpSetDefaultDs(instanceIndex, &default_ds);
	printf("SES_PtpSetDefaultDs - %d\n", rv);
 
	// Initialize port_ds with peer to peer delay
	TSN_ptp_port_ds_t port_ds;
	rv = SES_PtpGetPortDs(instanceIndex, 0, &port_ds);
	port_ds.delay_mechanism = TSN_ptp_delay_mechanism_p2p,
		port_ds.port_enable = 1;
 
 
	rv = SES_PtpSetPortDs(instanceIndex, 2, &port_ds);
	rv = SES_PtpSetPortDs(instanceIndex, 3, &port_ds);
 
	port_ds.mean_link_delay_thresh = 0xFA00000; // 4000ns * 2^16 
	rv = SES_PtpSetPortDs(instanceIndex, 0, &port_ds);
	rv = SES_PtpSetPortDs(instanceIndex, 1, &port_ds);
	rv = SES_PtpSetPortDs(instanceIndex, 4, &port_ds);
	rv = SES_PtpSetPortDs(instanceIndex, 5, &port_ds);
 
	return rv;
}

/* El ADIN1300 (RJ45, port5, addr MDIO 1) es CLAUSE-22: no responde a C45.
 * Sus registros MMD se alcanzan por acceso INDIRECTO via regs 0x0D/0x0E.
 */
static uint16_t rj_mmd_rd(uint16_t devad, uint16_t reg)
{
	uint16_t v = 0;
	SES_WritePhyReg(SES_macPort5, 0x000D, devad);
	SES_WritePhyReg(SES_macPort5, 0x000E, reg);
	SES_WritePhyReg(SES_macPort5, 0x000D, 0x4000 | devad);
	SES_ReadPhyReg(SES_macPort5, 0x000E, &v);
	return v;
}

static void rj_mmd_wr(uint16_t devad, uint16_t reg, uint16_t val)
{
	SES_WritePhyReg(SES_macPort5, 0x000D, devad);
	SES_WritePhyReg(SES_macPort5, 0x000E, reg);
	SES_WritePhyReg(SES_macPort5, 0x000D, 0x4000 | devad);
	SES_WritePhyReg(SES_macPort5, 0x000E, val);
}

int adin6310_enable_pse(struct device *ltc4296, uint8_t switch_op)
{
	/* PoDL is disabled for switch_op == 1 */
	if (FIELD_GET(BIT(0), switch_op) == 1)
		return 0;

	return device_init(ltc4296);
}

/* ===================================================================
 * DETECCION DE CORTOS EN MODULOS PSM/PDM + INDICACION POR LED
 * ===================================================================
 * Feature solicitada por Mayker (2026-07-23). Diagnostico STANDALONE
 * en campo, sin debugger: al arrancar (y refrescado periodicamente) se
 * prueba cada puerto PSE en modo clasificacion y se clasifica el par
 * como CORTO / ABIERTO / PD-presente. Un corto se senala parpadeando
 * un LED: N parpadeos = numero de SLOT en la SERIGRAFIA, pausa larga,
 * siguiente slot en corto, y asi. Sin cortos = LED apagado.
 *
 * Base fisica (medido por SWD el 2026-07-23, ver
 * ../PDM-SPE-NOTES.md y ../tools/ltc4296-swd/):
 *   - Puerto VACIO   -> sondeo Vout ~5100 mV (contra abierto)
 *   - PD sano        -> firma 4050-4550 mV, luego DELIVERING
 *   - Modulo en CORTO -> Vout ~70 mV, DET_VLOW perpetuo
 * El umbral de 2000 mV separa "corto" de todo lo demas con margen.
 *
 * Mapeo slot(serigrafia) <-> puerto LTC (confirmado con el PSM medido
 * en slot Port 4 = LTC_PORT2):
 *   Slot Port 1 = UPLINK (el PDM recibe PoDL; NO es salida PSE)
 *   Slot Port 2 = LTC_PORT0     Slot Port 4 = LTC_PORT2
 *   Slot Port 3 = LTC_PORT1     Slot Port 5 = LTC_PORT3
 *   Slot Port 6 = LTC_PORT4  (requiere habilitar el 5o puerto: ver README)
 *   => slot = LTC_port + 2
 *
 * Toda la API usada es publica (include/zephyr/drivers/sensor/ltc4296.h)
 * y NUNCA entrega potencia: solo prebias + clasificacion (uA) + disable.
 * =================================================================== */

/* --- Bits de PxST (replicados del driver privado ltc4296.h) --- */
#define MFS_PXST_PSE_STATUS_MSK   0x0007u
#define MFS_PXST_DET_VLOW_MSK     (1u << 12)
#define MFS_PXST_DET_VHIGH_MSK    (1u << 13)

/* --- Parametros ajustables --- */
#define MFS_SHORT_VOUT_MV   2000    /* Vout < esto en clasificacion = corto */
#define MFS_PSE_PORTS       5       /* puertos PSE configurados (LTC0..4).
                                     * El 5o (LTC_PORT4 = slot Port 6) se
                                     * habilito el 2026-07-29: hizo falta
                                     * tocar el DRIVER, no solo el dts. */
#define MFS_LTCPORT_TO_SLOT(p)  ((p) + 2)   /* serigrafia del slot */

/* Timings del patron de parpadeo (ms) */
#define MFS_BLINK_ON_MS     250
#define MFS_BLINK_OFF_MS    250
#define MFS_GAP_PORT_MS     1500    /* pausa larga entre slots en corto */
#define MFS_GAP_CYCLE_MS    3000    /* pausa entre ciclos completos */

/* Re-escaneo periodico (en iteraciones del bucle principal de 1s).
 * Detecta modulos cambiados/insertados sin necesidad de un POR. */
#define MFS_RESCAN_EVERY    30

/* --- Diagnostico visible por SWD --- */
/* Vigilante de bloqueo del LTC4296 y reintento de SPoE */
volatile unsigned short g_gcmd;                 /* ultimo GCMD leido */
volatile unsigned short g_unlock_n;             /* re-desbloqueos */
volatile int            g_retry_rc[MFS_PSE_PORTS];  /* rc del ultimo reintento */

volatile uint16_t g_short_mask;              /* bit p = LTC_PORTp en corto */
volatile int      g_short_vout[MFS_PSE_PORTS];

/* ============ TELEMETRIA: CORRIENTE Y POTENCIA POR SLOT ============
 * Espejo de la del MPS (`power-switch/src/main.c`, rama mps): el field switch
 * tampoco tiene pila de red y no le hace falta -- SES_XmitFrame() inyecta una
 * trama ya montada directamente en el switch, con la cabecera Ethernet a mano.
 *
 * Mismo ethertype 0x88B6 que el MPS; se distinguen por el magic. Difusion,
 * para que la TPU no tenga que conocer ninguna MAC de antemano.
 *
 * ⚠️ POR QUE LLEVA dev_id Y EL DEL MPS NO.
 * La MAC de esta placa SE SORTEA EN CADA ARRANQUE (mas abajo en main():
 * srand(k_cycle_get_32()); mac_addr[3..5] = rand()). Si la pasarela indexara
 * por MAC, cada reinicio de un field switch apareceria como un equipo NUEVO;
 * con 50 en campo el panel se llena de fantasmas en dias.
 *
 * Por eso la identidad viaja DENTRO de la trama y sale del USN del MAX32690,
 * que es unico por chip y sobrevive a reinicios y regrabados. La MAC se manda
 * igualmente, pero solo como dato informativo.
 * =================================================================== */
#define MFS_ETHERTYPE  0x88B6
#define MFS_MAGIC      0x4D465331u   /* "MFS1" */
#define MFS_TELE_TICKS 2             /* el bucle es de 1 s -> cada 2 s */
#define MFS_I_NA       ((int16_t)0x8000)  /* sin dato; espejo del MPS y la ATT */
#define MFS_VEC_POR_TRAMA 8   /* vecinos por trama; la tabla se recorre */

struct mfs_tele {
	uint8_t  dst[6];
	uint8_t  src[6];
	uint16_t ethertype;
	uint32_t magic;
	uint16_t version;
	uint16_t nports;
	uint32_t seq;
	uint32_t uptime_ms;
	uint64_t dev_id;                  /* identidad ESTABLE, del USN */
	int16_t  iout_ma[MFS_PSE_PORTS];  /* MFS_I_NA = sin lectura valida */
	uint16_t pxst[MFS_PSE_PORTS];     /* estado: distingue "no entrega" de 0 mA */
	/* diagnostico del chip, al final igual que en el MPS */
	uint16_t gcmd;
	uint16_t unlocks;
	int32_t  vin_mv;
	uint16_t vin_ok;
	uint16_t disc_n;
	/* --- v2: registro de fallos globales (GFLTEV). AL FINAL, como manda la
	 * convencion. Se anade porque NO PODER VERLO desde la pasarela fue lo que
	 * costo horas el 2026-08-04: un fallo de interruptor de baja enclavado
	 * impide reclasificar y desde fuera solo se ve "deshabilitado". */
	uint16_t gfltev;
	/* --- v3: VECINOS. Quien se ve por cada puerto del switch. -----------
	 * Con esto la pasarela reconstruye la JERARQUIA: que field switch cuelga
	 * de cual, y por que puerto. Sin esto el panel solo puede dar una lista
	 * plana de equipos, que con 50 tanques encadenados no sirve para
	 * diagnosticar nada.
	 *
	 * ⚠️ NO SE MANDA LA TABLA ENTERA. Detras de un switch encadenado puede
	 * haber decenas de MACs y la trama no puede crecer sin limite. Se mandan
	 * MFS_VEC_POR_TRAMA entradas por trama y se RECORRE la tabla en tramas
	 * sucesivas (idx0 dice por donde va). A 2 s por trama, una tabla de 50
	 * entradas queda cubierta en ~13 s. La pasarela acumula y envejece.
	 *
	 * La identidad la resuelve la pasarela: ya conoce la MAC de cada equipo
	 * porque la ve en sus tramas. Aqui solo se dice "esta MAC se ve por el
	 * puerto N". */
	uint16_t vec_total;                  /* entradas validas en la tabla */
	uint16_t vec_idx0;                   /* indice de la primera enviada */
	uint8_t  vec_n;                      /* cuantas van en ESTA trama */
	uint8_t  vec_pad;
	struct {
		uint8_t mac[6];
		uint8_t port;                /* portMap del switch */
		uint8_t pad;
	} vec[MFS_VEC_POR_TRAMA];
	/* --- v4: CORRIENTE SIN CONVERTIR. ------------------------------------
	 * `iout_ma` de arriba sale de una DIVISION ENTERA de C dentro del driver,
	 * que trunca hacia cero. Con el shunt de esta placa cada cuenta del ADC
	 * vale ~0.40 mA, asi que truncar tira hasta una cuenta entera y SIEMPRE
	 * hacia abajo: es un sesgo sistematico, no ruido, y se acumula al sumar
	 * la potencia de los cinco puertos.
	 *
	 * Se manda la cuenta cruda y el shunt; la conversion exacta la hace la
	 * pasarela en coma flotante. Mandar tambien el shunt vuelve la trama
	 * autodescriptiva: una placa con otro shunt no obliga a tocar la pasarela
	 * y un valor desactualizado no puede dar corrientes falsas creibles.
	 *
	 * `iout_ma` SE MANTIENE: un consumidor v3 sigue funcionando igual. */
	uint16_t adc_code[MFS_PSE_PORTS];  /* 12 bits, offset 2048; 0xFFFF = sin dato */
	uint16_t hs_res[MFS_PSE_PORTS];    /* shunt del puerto, como en el overlay */
} __packed;

/* ⚠️ CONTRATO CON LA PASARELA. `gateway.py` desempaqueta la carga (sin los 14
 * bytes de cabecera Ethernet) con:
 *
 *     MFS_FMT      = "!IHHIIQ" + "h"*5 + "H"*5     -> 44 bytes
 *     MFS_FMT_CHIP = "!HHiHH"                      -> 12 bytes
 *     MFS_FMT_V2   = "!H"   (gfltev)               ->  2 bytes
 *
 * 14 + 44 + 12 + 2 = 72. Si alguien toca esta estructura sin tocar el parser, el
 * fallo seria SILENCIOSO: tramas que se decodifican y dan corrientes absurdas.
 * Mejor que no compile.
 *
 *     v3: 6 + 8 * MFS_VEC_POR_TRAMA           (vecinos)
 *     v4: 4 * MFS_PSE_PORTS                   (adc_code + hs_res)
 */
BUILD_ASSERT(sizeof(struct mfs_tele) ==
		     72 + 6 + 8 * MFS_VEC_POR_TRAMA + 4 * MFS_PSE_PORTS,
	     "struct mfs_tele desalineada con MFS_FMT de gateway.py");

volatile int      g_tele_rc;      /* ultimo retorno de SES_XmitFrame */
volatile unsigned g_tele_n;       /* tramas emitidas */
volatile uint64_t g_dev_id;       /* identidad de esta placa (0 = sin USN) */
volatile unsigned short g_gfltev;  /* ultimo GFLTEV leido */
volatile unsigned short g_ckt_clr_n; /* veces que se limpio el interruptor */
volatile unsigned short g_vec_total; /* tamano observado de la tabla dinamica */
static sesID_t g_ses_dev;            /* id del switch, para leer su tabla */

/* Testigos del driver del LTC4296, igual que en el MPS. */
extern volatile int            g_ltc_vin_mv;
extern volatile unsigned char  g_ltc_vin_ok;
extern volatile unsigned short g_ltc_disc_n;

/* Identidad estable de la placa, derivada del USN del MAX32690.
 *
 * ⚠️ NO se usa MXC_SYS_GetUSN(): arrastra MXC_FLC_UnlockInfoBlock y MXC_CTB_Init
 * -- el driver de flash y el motor criptografico -- que este HAL no compila, y
 * el enlazado falla con "undefined reference". Ademas calcula un checksum por
 * hardware que aqui no hace ninguna falta.
 *
 * Se lee el bloque de informacion directamente. Es la MISMA secuencia que ya
 * usamos por SWD para inspeccionar la CRK (`FLASHEO.md` §4), aqui en tres
 * escrituras a `actrl`, sin dependencias de enlazado:
 *
 *     0x1234      asegurar bloqueado
 *     0x3a7f5ca3 / 0xa1e34f20 / 0x9608b2c1   secuencia de desbloqueo
 *     0xDEADBEEF  volver a bloquear
 *
 * ⚠️ SOLO LECTURA, y se re-bloquea SIEMPRE antes de salir. El bloque de
 * informacion contiene la CRK: dejarlo desbloqueado seria dejar la puerta
 * abierta a que un fallo posterior lo corrompa y la placa deje de arrancar
 * para siempre.
 *
 * Se pliega a 64 bits con FNV-1a en vez de truncar: los USN de un mismo lote
 * comparten prefijo, asi que quedarse con los primeros o ultimos bytes podria
 * colisionar entre placas hermanas -- justo el fallo que este campo existe
 * para evitar.
 *
 * Devuelve 0 si el bloque no da nada creible. La pasarela trata el 0 como "sin
 * identidad estable" y lo avisa en el panel, en vez de fingir que la tiene. */
#define MFS_USN_WORDS 4         /* palabras del info block que cubren el USN */

static uint64_t mfs_dev_id(void)
{
	volatile uint32_t *info = (volatile uint32_t *)MXC_INFO0_MEM_BASE;
	uint64_t h = 1469598103934665603ULL;   /* FNV-1a 64, offset basis */
	uint32_t w[MFS_USN_WORDS];
	int vacio = 1;

	MXC_FLC0->actrl = 0x1234;
	MXC_FLC0->actrl = 0x3a7f5ca3;
	MXC_FLC0->actrl = 0xa1e34f20;
	MXC_FLC0->actrl = 0x9608b2c1;

	for (int i = 0; i < MFS_USN_WORDS; i++) {
		w[i] = info[i];
	}

	MXC_FLC0->actrl = 0xDEADBEEF;          /* re-bloquear SIEMPRE */

	for (int i = 0; i < MFS_USN_WORDS; i++) {
		/* Un bloque a 0xFFFFFFFF o a 0 no es un numero de serie: es que no
		 * se pudo leer. Mejor devolver 0 que inventar una identidad. */
		if (w[i] != 0xFFFFFFFFu && w[i] != 0) {
			vacio = 0;
		}
		for (int b = 0; b < 4; b++) {
			h ^= (w[i] >> (8 * b)) & 0xFF;
			h *= 1099511628211ULL;         /* FNV-1a 64, primo */
		}
	}
	return vacio ? 0 : h;
}

static void mfs_tele_send(const struct device *ltc, const uint8_t mac[6])
{
	static uint32_t seq;
	static const enum ltc4296_port pp[MFS_PSE_PORTS] = {
		LTC_PORT0, LTC_PORT1, LTC_PORT2, LTC_PORT3, LTC_PORT4 };
	struct mfs_tele f;
	SES_transmitFrameData_t tx;

	memset(&f, 0, sizeof(f));
	memset(f.dst, 0xFF, sizeof(f.dst));            /* difusion */
	memcpy(f.src, mac, sizeof(f.src));
	f.ethertype = htons(MFS_ETHERTYPE);
	f.magic     = htonl(MFS_MAGIC);
	f.version   = htons(4);
	f.nports    = htons(MFS_PSE_PORTS);
	seq++;
	f.seq       = htonl(seq);
	f.uptime_ms = htonl((uint32_t)k_uptime_get_32());
	f.dev_id    = sys_cpu_to_be64(g_dev_id);

	for (int i = 0; i < MFS_PSE_PORTS; i++) {
		int ima = 0;
		uint16_t st = 0;

		/* ⚠️ El ADC de puerto solo da dato valido (bit NEW) en los puertos
		 * que entregan. Si falla se manda el centinela: un 0 seria un dato
		 * falso perfectamente creible, y en el panel se leeria como
		 * "conectado y sin consumo". */
		if (ltc4296_read_port_adc(ltc, pp[i], &ima) == 0) {
			if (ima >  32000) { ima =  32000; }
			if (ima < -32000) { ima = -32000; }
			f.iout_ma[i] = (int16_t)htons((uint16_t)(int16_t)ima);
		} else {
			f.iout_ma[i] = (int16_t)htons((uint16_t)MFS_I_NA);
		}

		if (ltc4296_read_port_status(ltc, pp[i], &st) != 0) { st = 0; }
		f.pxst[i] = htons(st);

		/* v4: la misma medida SIN convertir, para que la pasarela pueda
		 * dar el valor exacto en vez del truncado. 0xFFFF = sin lectura
		 * valida, mismo criterio que el centinela de iout_ma. */
		{
			uint16_t code = 0, res = 0;

			if (ltc4296_read_port_adc_raw(ltc, pp[i], &code, &res) == 0) {
				f.adc_code[i] = htons(code);
				f.hs_res[i]   = htons(res);
			} else {
				f.adc_code[i] = htons(0xFFFF);
				f.hs_res[i]   = htons(0);
			}
		}
	}

	f.gcmd    = htons(g_gcmd);
	f.unlocks = htons(g_unlock_n);
	f.vin_mv  = (int32_t)htonl((uint32_t)g_ltc_vin_mv);
	f.vin_ok  = htons(g_ltc_vin_ok);
	f.disc_n  = htons(g_ltc_disc_n);
	f.gfltev  = htons(g_gfltev);

	/* --- vecinos: un trozo de la tabla dinamica del switch --------------
	 * ⚠️ El indice de partida AVANZA en cada trama, para que a lo largo de
	 * varias se cubra la tabla entera sin engordar ninguna. */
	{
		static uint16_t vec_cursor;
		SES_dynTblEntry_t ent[MFS_VEC_POR_TRAMA];
		uint16_t validas = 0;
		int32_t rc;

		memset(ent, 0, sizeof(ent));
		rc = SES_MX_ReadDynamicTable(g_ses_dev, vec_cursor,
					     vec_cursor + MFS_VEC_POR_TRAMA - 1,
					     ent, &validas);
		if (rc != 0) {
			validas = 0;
		}
		if (validas > MFS_VEC_POR_TRAMA) {
			validas = MFS_VEC_POR_TRAMA;
		}
		f.vec_idx0 = htons(vec_cursor);
		f.vec_n    = (uint8_t)validas;
		f.vec_total = htons(g_vec_total);
		for (uint16_t i = 0; i < validas; i++) {
			memcpy(f.vec[i].mac, ent[i].macAddress, 6);
			f.vec[i].port = ent[i].portMap;
		}
		/* Si esta pasada devolvio menos de las pedidas, se acabo la tabla:
		 * se anota su tamano y se vuelve al principio. */
		if (validas < MFS_VEC_POR_TRAMA) {
			g_vec_total = vec_cursor + validas;
			vec_cursor = 0;
		} else {
			vec_cursor += validas;
		}
	}

	memset(&tx, 0, sizeof(tx));
	tx.frameType         = SES_standardFrame;
	tx.data_p            = &f;
	tx.byteCount         = sizeof(f);
	tx.ses.generateFcs   = 1;
	tx.ses.egressPortMap = 0xFF;   /* por todos: la TPU puede colgar de cualquiera */

	g_tele_rc = SES_XmitFrame(&tx);
	if (g_tele_rc == 0) { g_tele_n++; }
}

/* Prueba un puerto en modo clasificacion y devuelve el Vout de sondeo.
 * Deja el puerto DESHABILITADO al salir (jamas entrega potencia).
 * Devuelve el Vout en mV (puede ser negativo/absurdo si el ADC del chip
 * no dio dato valido: el llamante lo filtra). */
static int mfs_probe_port_vout(const struct device *dev, enum ltc4296_port p)
{
	int vout_mv = 0;

	ltc4296_port_prebias(dev, p, LTC_CFG_SCCP_MODE);   /* PxCFG1 = 0x0108 */
	ltc4296_port_en_and_classification(dev, p);        /* PxCFG0 = 0x2041 */
	k_sleep(K_MSEC(30));                               /* asentar sondeo   */
	ltc4296_set_gadc_vout(dev, p);
	k_sleep(K_MSEC(10));
	ltc4296_read_gadc(dev, &vout_mv);
	ltc4296_port_disable(dev, p);                      /* PxCFG0 = 0x0000  */

	return vout_mv;
}

/* Recorre los puertos PSE y marca cuales estan en corto.
 * SALTA los puertos que estan DELIVERING (PD real alimentandose): no se
 * deben sondear para no interrumpir la entrega, y obviamente no son corto.
 * Actualiza g_short_mask / g_short_vout y devuelve la mascara. */
/* ⚠️ SONDEO DE CORTOS Y NEGOCIACION VAN EN PASADAS DISTINTAS.
 *
 * ESTE ES EL FALLO QUE HIZO QUE UN SLOT NO SE RECUPERARA NUNCA (2026-08-04).
 *
 * mfs_probe_port_vout() mete el puerto en modo CLASIFICACION para medir Vout.
 * Eso toca la linea SCCP. Llamar a la negociacion inmediatamente despues, sobre
 * la misma linea que no ha vuelto a reposo, la hace fallar SIEMPRE:
 *
 *   Port0 Vin 53338V
 *   PD classification failed, PD line not HIGH     <- resto del sondeo
 *   PD detection failed, PD line not LOW           <- la negociacion, ya perdida
 *   SCCP.. PD Not Present
 *
 * Cuatro mensajes de linea en 240 ms = DOS intercambios SCCP pisandose.
 *
 * Por eso el arranque siempre funcionaba y el reintento nunca: probe() llama a
 * do_spoe_sccp() sobre un puerto LIMPIO, sin sondear cortos antes. La diferencia
 * no estaba en el chip ni en el PD -- estaba aqui.
 *
 * Se alternan las pasadas: una sondea cortos, la siguiente reintenta. Nunca las
 * dos sobre el mismo puerto en la misma vuelta. La deteccion de cortos se
 * conserva intacta -- existe para no energizar un puerto en cortocircuito -- solo
 * que ahora corre cada dos ciclos (60 s en vez de 30 s), que sobra.
 */
static uint16_t mfs_scan_shorts(const struct device *dev)
{
	static unsigned pasada;
	/* false = sondear cortos; true = reintentar negociacion */
	const bool reintentar = (pasada++ & 1u) != 0;
	uint16_t mask = reintentar ? g_short_mask : 0;
	uint16_t gf = 0;
	/* BIT(0) de GFLTEV. Vive en el .h PRIVADO del driver
	 * (drivers/sensor/ltc4296/ltc4296.h:70) y no en la cabecera publica,
	 * asi que se repite aqui en vez de incluir cabeceras internas. */
	const uint16_t LOW_CKT_BRK = BIT(0);

	/* ⚠️ LIMPIAR EL INTERRUPTOR DE BAJA ANTES DE REINTENTAR.
	 *
	 * Con un LTC4296_LOW_CKT_BRK_FAULT enclavado, el puerto NO vuelve a
	 * clasificar por mucho que se reintente la negociacion: desde fuera solo
	 * se ve "deshabilitado" para siempre.
	 *
	 * Lo dispara que la carga desaparezca de golpe -- exactamente lo que pasa
	 * cuando una ATT pasa a bateria. Verificado el 2026-08-04 en tank1: el
	 * slot quedo muerto y NO se recupero en 20 minutos de reintentos; al
	 * reiniciar SOLO el field switch, clasifico al instante. La unica
	 * diferencia era que el arranque limpia los fallos y el reintento no.
	 *
	 * ⚠️ Se limpia SOLO ese bit, no se llama a ltc4296_chk_global_events():
	 * esa funcion tiene ramas que hacen ltc4296_reset() y TIRARIAN la entrega
	 * de los puertos vivos. Escribir un 1 en LOW_CKT_BRK_FAULT es idempotente
	 * y no molesta a nadie.
	 *
	 * (Aqui se corrige un juicio previo: se habia descartado la recuperacion
	 * del fabricante EN BLOQUE por hacer reset, y eso solo vale para algunas
	 * de sus ramas.) */
	if (ltc4296_read_global_faults(dev, &gf) == 0) {
		g_gfltev = gf;
		if (gf & LOW_CKT_BRK) {
			if (ltc4296_clear_ckt_breaker(dev) == 0) {
				g_ckt_clr_n++;
				LOG_WRN("LTC4296: interruptor de baja enclavado"
					" (GFLTEV=0x%04x), limpiado antes de reintentar", gf);
			}
		}
	}

	for (int p = 0; p < MFS_PSE_PORTS; p++) {
		enum ltc4296_pse_status pwr = LTC_PSE_STATUS_UNKNOWN;

		/* no molestar a un puerto entregando a un PD real */
		if (ltc4296_is_port_deliver_pwr(dev, (enum ltc4296_port)p, &pwr) == 0 &&
		    pwr == LTC_PSE_STATUS_DELIVERING) {
			g_short_vout[p] = -1;   /* marca "entregando", no sondeado */
			continue;
		}

		if (!reintentar) {
			/* --- PASADA DE SONDEO: solo medir, no negociar --- */
			int v = mfs_probe_port_vout(dev, (enum ltc4296_port)p);

			g_short_vout[p] = v;

			/* corto = Vout bajo Y positivo. Un valor <0 o muy grande
			 * (p.ej. +/-71000 con SPI/ADC mudo) NO es corto: se ignora
			 * para evitar falsos positivos. */
			if (v >= 0 && v < MFS_SHORT_VOUT_MV) {
				mask |= (1u << p);
			}
			continue;
		}

		/* --- PASADA DE NEGOCIACION: la linea lleva 30 s en reposo ---
		 *
		 * ⚠️ Un puerto marcado en corto en la pasada anterior NO se toca:
		 * negociar es aplicar tension, y sobre un cortocircuito eso es
		 * justo lo que la deteccion existe para evitar.
		 *
		 * do_spoe_sccp() solo negocia en su rama `port_chk ==
		 * LTC_PORT_DISABLED`. Un puerto que no entrega ya esta ahi: el
		 * propio driver lo deshabilita en todos sus caminos de fallo. */
		if (mask & (1u << p)) {
			continue;
		}
		{
			struct ltc4296_vi vi;

			g_retry_rc[p] = ltc4296_retry_spoe_sccp(
				dev, (enum ltc4296_port)p, &vi);
		}
	}

	g_short_mask = mask;
	return mask;
}

/* Emite UN ciclo del patron: por cada puerto en corto, (slot) parpadeos
 * + pausa larga. El LED es activo-alto (GPIO -> FET -> LED). */
static void mfs_blink_shorts(const struct gpio_dt_spec *led, uint16_t mask)
{
	for (int p = 0; p < MFS_PSE_PORTS; p++) {
		if (!(mask & (1u << p)))
			continue;

		int slot = MFS_LTCPORT_TO_SLOT(p);   /* numero de serigrafia */
		for (int b = 0; b < slot; b++) {
			gpio_pin_set_dt(led, 1);
			k_sleep(K_MSEC(MFS_BLINK_ON_MS));
			gpio_pin_set_dt(led, 0);
			k_sleep(K_MSEC(MFS_BLINK_OFF_MS));
		}
		k_sleep(K_MSEC(MFS_GAP_PORT_MS));    /* pausa larga entre slots */
	}
	if (mask)
		k_sleep(K_MSEC(MFS_GAP_CYCLE_MS));   /* pausa entre ciclos */
}

int main(void)
{
	int32_t ret;
	int iface;
	sesID_t dev_id;
	uint8_t switch_val;
	uint8_t switch_op = 0;
	struct k_thread thread_data;
	struct gpio_callback cb_data;
	struct ltc4296_vi ltc4296_voltage;
	struct ltc4296_dev_config *ltc4296_config;
	uint8_t mac_addr[6] = {0x00, 0x18, 0x80, 0x03, 0x25, 0x60};

	const struct device *const ltc4296_dev = DEVICE_DT_GET(DT_NODELABEL(ltc4296));
	const SES_portInit_t initializePorts_p[] = {
		/* FIELD SWITCH (MFS): los 6 puertos son SPE (ADIN1100 en RMII).
		 * No hay uplink RGMII ni RJ45: es un switch SPE puro.
		 * Los puertos SPE de esta placa son RMII, NO RGMII: el ADIN6310
		 * entrega el reloj de referencia de 50MHz comun. Con RGMII los
		 * ADIN1100 se quedan sin reloj y su lado de linea queda muerto.
		 *
		 * phyAddr: PROVISIONAL. La fija el DIP de cada modulo y aqui no la
		 * conocemos: el escaneo MDIO del arranque (g_map) dira las reales.
		 */
		{ 1, SES_rmiiMode, { 0, 0, 0 }, 1, SES_phyADIN1100, {true, 1, 0, SES_phySpeed10, SES_phyDuplexModeFull, SES_autoMdix}},
		{ 1, SES_rmiiMode, { 0, 0, 0 }, 1, SES_phyADIN1100, {true, 1, 1, SES_phySpeed10, SES_phyDuplexModeFull, SES_autoMdix}},
		{ 1, SES_rmiiMode, { 0, 0, 0 }, 1, SES_phyADIN1100, {true, 1, 2, SES_phySpeed10, SES_phyDuplexModeFull, SES_autoMdix}},
		{ 1, SES_rmiiMode, { 0, 0, 0 }, 1, SES_phyADIN1100, {true, 1, 3, SES_phySpeed10, SES_phyDuplexModeFull, SES_autoMdix}},
		{ 1, SES_rmiiMode, { 0, 0, 0 }, 1, SES_phyADIN1100, {true, 1, 4, SES_phySpeed10, SES_phyDuplexModeFull, SES_autoMdix}},
		{ 1, SES_rmiiMode, { 0, 0, 0 }, 1, SES_phyADIN1100, {true, 1, 5, SES_phySpeed10, SES_phyDuplexModeFull, SES_autoMdix}}
	};

	SES_driverFunctions_t comm_callbacks = {
		.init_p = SES_PORT_SPI_Init,
		.release_p = SES_PORT_SPI_Release,
		.sendMessage_p = adin6310_spi_write,
	};

	struct gpio_dt_spec reset_gpio = GPIO_DT_SPEC_GET(DT_NODELABEL(adin6310), reset_gpios);
	struct gpio_dt_spec irq_gpio = GPIO_DT_SPEC_GET(DT_NODELABEL(adin6310), int_gpios);
	struct gpio_dt_spec config_gpio[4] = {
		GPIO_DT_SPEC_GET_BY_IDX(DT_NODELABEL(adin6310), cfg_gpios, 0),
		GPIO_DT_SPEC_GET_BY_IDX(DT_NODELABEL(adin6310), cfg_gpios, 1),
		GPIO_DT_SPEC_GET_BY_IDX(DT_NODELABEL(adin6310), cfg_gpios, 2),
		GPIO_DT_SPEC_GET_BY_IDX(DT_NODELABEL(adin6310), cfg_gpios, 3),
	};

	ret = gpio_pin_configure_dt(&irq_gpio, GPIO_INPUT);
	if (ret)
		return ret;

	ret = gpio_pin_configure_dt(&reset_gpio, GPIO_OUTPUT_INACTIVE);
        if (ret) {
        	LOG_ERR("Failed to configure reset GPIO, %d", ret);
		return ret;
        }

	for (int i = 0; i < ARRAY_SIZE(config_gpio); i++) {
		ret = gpio_pin_configure_dt(&config_gpio[i], GPIO_INPUT);
		if (ret)
			return ret;
		
		switch_val = gpio_pin_get_dt(&config_gpio[i]);
		switch_op |= !!switch_val << i;
	}

	/* reset pulse removed: P1.8 drives board RESET_N via AND gate */
        k_busy_wait(1000);
        k_busy_wait(1000);

	/* El arranque del lector se ha movido tras SES_AddHwInterface: aqui
	 * llegaba cientos de ms antes de SES_Init y los mensajes de esa ventana
	 * se perdian (SES_ReceiveMessage los rechaza sin libreria inicializada). */

	k_sleep(K_MSEC(1));
	/* ⚠️ La MAC se SORTEA en cada arranque. No se toca aqui para no cambiar el
	 * comportamiento de red probado en campo, pero es la razon de que la
	 * telemetria lleve `dev_id`: la identidad de esta placa NO puede ser su
	 * MAC. Ver el comentario de struct mfs_tele.
	 *
	 * (Pendiente de valorar: derivar tambien la MAC del USN, para que deje de
	 * cambiar en cada arranque en una red puenteada con la de oficina.) */
	srand(k_cycle_get_32());
	mac_addr[3] = rand();
	mac_addr[4] = rand();
	mac_addr[5] = rand();

	g_dev_id = mfs_dev_id();
	printf("dev_id (USN): %08x%08x\n",
	       (unsigned)(g_dev_id >> 32), (unsigned)(g_dev_id & 0xFFFFFFFFu));

	ret = adin6310_enable_pse(ltc4296_dev, switch_op);
	if (ret){
		printf("Could not initialize %s\n", ltc4296_dev->name);
	}

	ltc4296_config = ltc4296_dev->config;

	ret = SES_Init();
	if (ret)
		printf("SES_Init() error\n");

	ret = SES_AddHwInterface(NULL, &comm_callbacks, &iface);
	if (ret) {
		printf("SES_AddHwInterface() error\n");
		return ret;
	}

	/* Ahora si: SES esta inicializado y puede aceptar mensajes. */
	k_tid_t spi_read_tid = k_thread_create(&thread_data, stack_area,
					       K_THREAD_STACK_SIZEOF(stack_area),
					       adin6310_msg_recv, NULL, NULL, NULL,
					       K_PRIO_PREEMPT(0), 0, K_FOREVER);

	k_thread_name_set(spi_read_tid, "ADIN6310 SPI reader");
	/* The SPI is not initialized, so we may start the thread. */
	k_thread_start(spi_read_tid);
	gpio_init_callback(&cb_data, adin6310_int_rdy, BIT(irq_gpio.pin));
	gpio_add_callback(irq_gpio.port, &cb_data);
	ret = gpio_pin_interrupt_configure_dt(&irq_gpio, GPIO_INT_EDGE_TO_ACTIVE);
	if (ret)
		return ret;

	/* La interrupcion es POR FLANCO: si el switch ya la tenia activa antes de
	 * armarla, ese flanco no existe y el mensaje pendiente no se leeria nunca.
	 * Se fuerza una lectura para drenarlo. */
	k_sem_give(&reader_thread_sem);
	k_sleep(K_MSEC(10));

	ret = SES_AddDevice(iface, mac_addr, &dev_id);
	if (ret) {
		printf("SES_AddDevice() error %d\n", ret);
		return ret;
	}

	/* El emisor de telemetria necesita este id para leer la tabla dinamica
	 * del switch (los vecinos por puerto, para la jerarquia). */
	g_ses_dev = dev_id;

	ret = SES_MX_InitializePorts(dev_id, 6, initializePorts_p);
	if (ret) {
		printf("SES_MX_InitializePorts() error %d\n", ret);
		return ret;
	}


	ret = SES_FirmwareCheck();
	
	printf("Configured MAC address: ");
	for (int i = 0; i < 5; i++)
		printf("%02x:", mac_addr[i]);

	printf("%02x\n", mac_addr[5]);

	if (switch_op & BIT(0)) {
		printf("PSE disabled\n");
		ret = adin6310_vlan_example();
		if (ret) {
			printf("VLAN init error!\n");
			return ret;
		}
	} else {
		printf("PSE enabled\n");
		ret = adin6310_vlan_example();
		if (ret) {
			printf("VLAN init error!\n");
			return ret;
		}
	}

	if (switch_op & BIT(1)){
		printf("Time Synchonization example\n");
		ret = timesync(mac_addr);
		if (ret) {
			printf("Could not initialize Time Sync\n");
			return ret;
		}
	}

	if (switch_op & BIT(2)){
		printf("LLDP Protcol\n");
		if (SES_LLDP_Init() != 1)
		{		
			ret = SES_LLDP_Start();
		}
		if (ret) {
			printf("LLDP Init Error!!\n");
			return ret;
		}
	}

	if (switch_op & BIT(3)){
		printf("IGMP Snooping\n");
		ret = SES_IgmpEnable();
		if (ret) {
			printf("IGMP init error\n");
			return ret;
		}
	}

	printf("Configuration done\n");

	/* Deteccion de cortos PSM/PDM + LED (feature Mayker). Sustituye al bloque
	 * de sondeo RJ45/ADIN1300 del macPort5 (hardware inexistente en el field
	 * switch, todo SPE) que crasheaba la app. */
	struct gpio_dt_spec fault_led = GPIO_DT_SPEC_GET(DT_NODELABEL(mfs_fault_led), gpios);
	gpio_pin_configure_dt(&fault_led, GPIO_OUTPUT_INACTIVE);
	mfs_scan_shorts(ltc4296_dev);

	unsigned int mfs_pse_tick = 0;

	while (1) {
		k_sleep(K_MSEC(1000));
		g_link[0] = SES_GetLinkState(SES_macPort0);
		g_link[1] = SES_GetLinkState(SES_macPort1);
		g_link[2] = SES_GetLinkState(SES_macPort2);
		g_link[3] = SES_GetLinkState(SES_macPort3);
		g_link[4] = SES_GetLinkState(SES_macPort4);
		g_link[5] = SES_GetLinkState(SES_macPort5);

		/* Registros del PHY de los seis puertos. g_link no distingue
		 * "el SES no ve el PHY" de "el PHY esta pero sin portadora", y
		 * ademas da links fantasma en los slots vacios. Esto si.
		 *
		 * Cada 10 s, no cada segundo: son 36 lecturas MDIO por vuelta.
		 * Se conserva porque es el unico diagnostico sin depurador. */
		static int mfs_phy_tick = 0;
		if (++mfs_phy_tick >= 10) {
			mfs_phy_tick = 0;
			static const SES_mac_t phl[6] = {
				SES_macPort0, SES_macPort1, SES_macPort2,
				SES_macPort3, SES_macPort4, SES_macPort5 };
			uint16_t v;

			for (int q = 0; q < 6; q++) {
				v = 0; g_ph_rc[q] = SES_ReadPhyReg(phl[q], 0x010002, &v); g_ph_id1[q]   = v;
				v = 0; SES_ReadPhyReg(phl[q], 0x010003, &v);              g_ph_id2[q]   = v;
				v = 0; SES_ReadPhyReg(phl[q], 0x0108F7, &v);              g_ph_b10l[q]  = v;
				v = 0; SES_ReadPhyReg(phl[q], 0x070201, &v);              g_ph_anst[q]  = v;
				v = 0; SES_ReadPhyReg(phl[q], 0x070200, &v);              g_ph_anctl[q] = v;
				v = 0; SES_ReadPhyReg(phl[q], 0x010834, &v);              g_ph_pma[q]   = v;
			}
		}

		/* ⚠️ VIGILANTE DE BLOQUEO DEL LTC4296.
		 * El chip se re-bloquea solo y un chip bloqueado IGNORA LAS
		 * ESCRITURAS EN SILENCIO. Aqui importa el doble: sin esto no solo
		 * deja de negociar, es que mfs_scan_shorts() tambien dejaria de
		 * funcionar sin dar ningun error.
		 *
		 * NO se llama a ltc4296_chk_global_events() (la recuperacion del
		 * fabricante, sin un solo llamante en todo el arbol): hace
		 * ltc4296_reset(), que TIRARIA la entrega de los puertos vivos.
		 * Reescribir la llave es idempotente.
		 *
		 * Verificado que no da problemas: con esto activo y el reintento
		 * fuera, la placa arranca y sigue viva (bisect 2026-08-04). */
		if (++mfs_pse_tick >= 5) {
			uint16_t gc = 0;

			mfs_pse_tick = 0;

			if (ltc4296_reg_read(ltc4296_dev, 0x08, &gc) == 0) {
				if ((gc & 0x05) != 0x05) {
					ltc4296_unlock(ltc4296_dev);
					g_unlock_n++;
					ltc4296_reg_read(ltc4296_dev, 0x08, &gc);
				}
				g_gcmd = gc;
			}
		}

		/* Telemetria de consumo por slot hacia la pasarela.
		 *
		 * Va DESPUES del vigilante a proposito: si el chip estaba bloqueado,
		 * el vigilante acaba de reescribir la llave, asi que las lecturas de
		 * esta vuelta ya son buenas en vez de ser silenciosamente basura. */
		{
			static unsigned mfs_tele_tick;

			if (++mfs_tele_tick >= MFS_TELE_TICKS) {
				mfs_tele_tick = 0;
				mfs_tele_send(ltc4296_dev, mac_addr);
			}
		}

		{
			static int mfs_rescan = 0;
			if (++mfs_rescan >= MFS_RESCAN_EVERY) { mfs_rescan = 0; mfs_scan_shorts(ltc4296_dev); }
			if (g_short_mask)
				mfs_blink_shorts(&fault_led, g_short_mask);
			else
				gpio_pin_set_dt(&fault_led, 0);
		}
	}

	return 0;
}
