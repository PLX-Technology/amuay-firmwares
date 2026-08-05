/*
 * Copyright (c) 2024 Analog Devices Inc.
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include <zephyr/logging/log.h>
LOG_MODULE_REGISTER(eth_adin6310, CONFIG_LOG_DEFAULT_LEVEL);

#include <zephyr/kernel.h>
#include <stdio.h>
#include <string.h>
#include <zephyr/device.h>
#include <zephyr/net/net_config.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/drivers/spi.h>
#include <zephyr/drivers/sensor/ltc4296.h>
#include <zephyr/sys/byteorder.h>
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
/* Estado de enlace por puerto, para inspeccion por SWD (no hay consola). */
volatile int g_link[6];
/* Diagnostico PSE (temporal, no commitear). */
volatile unsigned int g_pse_initret;
volatile unsigned short g_pse_gcmd, g_pse_gflt;
volatile unsigned short g_pse_stat[4], g_pse_evt[4];
volatile int g_pse_iout[4];
volatile unsigned short g_pse2_gflt;
volatile unsigned short g_pse2_stat[4], g_pse2_evt[4];
volatile int g_pse2_iout[4];
volatile int g_pse2_try[4];
volatile int g_vin_pre;      /* VIN antes de energizar, mV */
volatile int g_vin_post;     /* VIN despues, mV */
volatile int g_vout_mv[4];   /* VOUT por puerto, mV */
volatile int g_vrc[4];
volatile unsigned short g_cfg0[4], g_cfg1[4];
volatile int g_sccp_rc[4];    /* retorno de do_spoe_sccp por puerto */
volatile int g_pd_present[4]; /* pd_present del pulso SCCP */
volatile int g_rst_rc[4];     /* retorno de sccp_reset_pulse */
volatile int g_vi_vin[4], g_vi_vout[4];
volatile unsigned short g_gcap, g_gcfg, g_giost;
volatile unsigned short g_gflt_pre, g_gflt_post;
volatile unsigned short g_cfgA[4];              /* CFG0 tras prebias */
volatile unsigned short g_cfgB[4], g_stB[4];    /* inmediato tras enable */
volatile unsigned short g_cfgC[4], g_stC[4];    /* +4 ms  */
volatile unsigned short g_cfgD[4], g_stD[4];    /* +100 ms */
/* medida de la linea SCCP del puerto 3 (el cableado al field switch) */
volatile int g_gpio_ok;
volatile int g_raw_drive_hi;   /* sccpi leido con sccpo fisicamente ALTO */
volatile int g_raw_drive_lo;   /* sccpi leido con sccpo fisicamente BAJO */
volatile int g_log_release;    /* sccpi tras RELEASE_LINE (logico 1) */
volatile int g_log_pulldown;   /* sccpi tras PULL_DOWN_LINE (logico 0) */
volatile int g_idle_hiz;       /* sccpi con sccpo en alta impedancia */
/* linea SCCP medida con el puerto en SEARCHING */
volatile unsigned short g_srch_st;
volatile int g_srch_hiz, g_srch_hi, g_srch_lo;
/* entrega forzada */
volatile unsigned short g_f_st[4], g_f_ev[4];
volatile int g_f_vout[4], g_f_iout[4];
volatile unsigned short g_f_gflt;
volatile unsigned short g_gcap2;
volatile unsigned short g_s1[4], g_s2[4], g_s3[4], g_s4[4];  /* STAT por paso */
volatile unsigned short g_e4[4];                              /* EVT final */
volatile int g_v4[4], g_i4[4];                                /* VOUT/IOUT final */
/* entrega sostenida, sin port_pwr_available */
volatile unsigned short g_h_st[4][3];   /* STAT a 100ms / 1s / 3s */
volatile int g_h_vout[4][3];            /* VOUT en los mismos instantes */
volatile int g_h_iout[4][3];
volatile unsigned short g_h_cfg0[4], g_h_ev[4];
/* A = sin clasificacion; B = ademas con SW_INRUSH */
volatile unsigned short g_A_st[4][2], g_B_st[4][2];
volatile int g_A_vout[4][2], g_B_vout[4][2];
volatile unsigned short g_A_cfg[4], g_B_cfg[4], g_A_ev[4], g_B_ev[4];
/* pasadas: 0 = GCFG tal cual, 1 = +MASK_LOWFAULT, 2 = +MASK_LOWFAULT|TLIM_DISABLE */
volatile unsigned short g_m_gcfg[3];      /* GCFG leido de vuelta */
volatile unsigned short g_m_st[3][4];     /* PXST por pasada y puerto */
volatile unsigned short g_m_ev[3][4];
volatile int g_m_vout[3][4];
volatile unsigned short g_m_gflt[3];
/* combinacion: mascara + secuencia que alcanzo DELIVERING */
volatile unsigned short g_c_gcfg;
volatile unsigned short g_c_st[4][4];   /* PXST a 50ms/300ms/1s/3s */
volatile int g_c_vout[4][4];
volatile unsigned short g_c_ev[4], g_c_cfg0[4];
volatile int g_c_iout[4];
/* A = solo el puerto 3 encendido; B = los 4 a la vez (control) */
volatile unsigned short g_solo_st[8], g_todos_st[8];   /* PXST en 8 instantes */
volatile int g_solo_vout[8], g_todos_vout[8];
volatile unsigned short g_solo_ev, g_todos_ev;
/* configuracion segun datasheet */
volatile unsigned short g_ds_cfg0[4], g_ds_cfg1[4], g_ds_adccfg[4], g_ds_gcfg;
volatile unsigned short g_ds_st[4][6];   /* PXST a 5/20/100/500/1500/3000 ms */
volatile int g_ds_vout[4][6];
volatile unsigned short g_ds_ev[4];
volatile int g_ds_iout[4];
/* confirmacion: 4 puertos a la vez, ADC con asentamiento correcto */
volatile unsigned short g_ok_st[4][3];
volatile int g_ok_vout[4][3], g_ok_iout[4][3];
volatile unsigned short g_ok_ev[4];
/* diagnostico linea de baja, SIN MASK_LOWFAULT */
volatile unsigned short g_ls_gfltev_pre, g_ls_gfltev_post, g_ls_gcfg;
volatile unsigned short g_ls_st[4], g_ls_ev[4];   /* PxEV bit0=LSNS_REV bit1=LSNS_FWD */
volatile int g_ls_vout[4], g_ls_iout[4];
/* SOLUCION: soft-start y foldback activos, sin mask de baja */
volatile unsigned short g_fx_gcfg, g_fx_gfltev;
volatile unsigned short g_fx_st[4][3], g_fx_ev[4];
volatile int g_fx_vout[4][3], g_fx_iout[4][3];
volatile int g_rc2[4];                          /* rc de do_spoe_sccp ya con faults limpios */
volatile unsigned short g_fcfg0[4], g_fst[4], g_fev[4];
volatile int g_fvout[4], g_fiout[4];
/* Paso lado bajo (Mayker): re-arme/verificacion del LGATE tras negociar */
volatile int g_lg_deliver[4];         /* 1 = puerto entregando (negocio OK) */
volatile int g_lg_any;                /* 1 = al menos un puerto negocio */
volatile int g_lg_rearm_rc;           /* retorno de clear_ckt_breaker (-1 = no negocio) */
volatile unsigned short g_lg_gfltev;  /* GFLTEV tras re-arme (bit0 = LOW_CKT_BRK_FAULT) */
volatile unsigned short g_lg_pxev[4]; /* PxEV por puerto (bit0 LSNS_REV, bit1 LSNS_FWD) */
volatile unsigned short g_lg_st[4];   /* PxST por puerto */
volatile int g_retry_rc[4];           /* rc de retry_spoe_sccp en el bucle (hot-plug) */       /* codigos de retorno de la medida */

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

/* ============ TELEMETRIA: CORRIENTE POR PUERTO DEL LTC4296 ============
 * El MPS no tiene pila de red y no le hace falta: SES_XmitFrame() inyecta una
 * trama ya montada directamente en el switch. Se arma la cabecera Ethernet a
 * mano.
 *
 * Ethertype propio 0x88B6, al lado del 0x88B5 que usa la ATT para sus tramas
 * de nivel, para que la pasarela pueda distinguirlas sin ambiguedad.
 *
 * Destino difusion: la TPU no necesita conocer ninguna MAC de antemano, y el
 * switch la hace llegar por donde este conectada.
 * ==================================================================== */
#define MPS_ETHERTYPE  0x88B6
#define MPS_MAGIC      0x4D505331u   /* "MPS1" */
#define MPS_TELE_TICKS 2             /* el bucle es de 1 s -> cada 2 s */
#define MPS_I_NA       ((int16_t)0x8000)  /* sin dato; espejo del ATT_NA */
#define MPS_VEC_POR_TRAMA 8   /* vecinos por trama; la tabla se recorre */

struct mps_tele {
	uint8_t  dst[6];
	uint8_t  src[6];
	uint16_t ethertype;
	uint32_t magic;
	uint16_t version;
	uint16_t nports;
	uint32_t seq;
	uint32_t uptime_ms;
	int16_t  iout_ma[4];   /* MPS_I_NA = el puerto no da lectura valida */
	uint16_t pxst[4];      /* estado, para distinguir "no entrega" de "0 mA" */
	/* --- v2: diagnostico del chip. Van al final a proposito: un parser v1
	 * desempaqueta solo los primeros bytes y no se entera. --- */
	uint16_t gcmd;         /* GCMD; 0x05 = desbloqueado */
	uint16_t unlocks;      /* veces que hubo que reescribir la llave */
	int32_t  vin_mv;       /* Vin de la ultima clasificacion; -1 = ninguna aun */
	uint16_t vin_ok;       /* 1 = ese Vin estaba en rango */
	uint16_t disc_n;       /* clasificaciones abandonadas por Vin fuera de rango */
	/* --- v3: identidad estable, del USN del MAX32690. -------------------
	 * ⚠️ VA AL FINAL, DESPUES del bloque v2, y no es capricho: la convencion
	 * de este proyecto es que cada version ANADE campos al final y jamas
	 * reordena. Insertarlo en medio dejaria de golpe a toda pasarela v2
	 * leyendo corrientes desplazadas -- basura perfectamente creible.
	 *
	 * Por que hace falta: la MAC del MPS esta CABLEADA en el firmware
	 * (mps_mac, justo debajo), asi que dos power switches colisionarian en el
	 * registro de la pasarela. Con dev_id cada uno tiene identidad propia.
	 * 0 = no se pudo leer; la pasarela lo marca como identidad no estable. */
	uint64_t dev_id;
	/* --- v4: VECINOS. Quien se ve por cada puerto del switch. ------------
	 * Gemelo del bloque v3 de `struct mfs_tele` (rama mfs). El MPS es la RAIZ
	 * del arbol: sin esto la pasarela sabe que field switch cuelga de cual,
	 * pero no de que SLOT del power switch cuelga la rama entera, y el panel
	 * enseña dos arboles sueltos en vez de uno.
	 *
	 * ⚠️ NO SE MANDA LA TABLA ENTERA: detras de cada slot puede haber decenas
	 * de MACs. Se mandan MFS_VEC_POR_TRAMA entradas y se RECORRE la tabla en
	 * tramas sucesivas (idx0 dice por donde va). La pasarela acumula y
	 * envejece las entradas.
	 *
	 * ⚠️ `port` es el `portMap` CRUDO del switch, que es una MASCARA DE BITS
	 * (bit N = macPort N), no un indice. Se manda tal cual y lo decodifica la
	 * pasarela; mandarlo ya decodificado obligaria a reflashear para corregir
	 * un mapeo. Tomarlo por indice fue el fallo del 2026-08-05. */
	uint16_t vec_total;                  /* entradas validas en la tabla */
	uint16_t vec_idx0;                   /* indice de la primera enviada */
	uint8_t  vec_n;                      /* cuantas van en ESTA trama */
	uint8_t  vec_pad;
	struct {
		uint8_t mac[6];
		uint8_t port;                /* portMap del switch (mascara) */
		uint8_t pad;
	} vec[MPS_VEC_POR_TRAMA];
	/* --- v5: CORRIENTE SIN CONVERTIR. Gemelo del bloque v4 del MFS. -------
	 * `iout_ma` de arriba sale de una DIVISION ENTERA de C dentro del driver,
	 * que trunca hacia cero. Con el shunt de esta placa (270) cada cuenta del
	 * ADC vale ~0.37 mA, asi que truncar tira hasta una cuenta entera y
	 * SIEMPRE hacia abajo: sesgo sistematico, no ruido.
	 *
	 * Se manda la cuenta cruda MAS el shunt con el que convertirla; la cuenta
	 * exacta la hace la pasarela en coma flotante. `iout_ma` se mantiene: un
	 * consumidor v4 sigue funcionando igual. */
	uint16_t adc_code[4];   /* 12 bits, offset 2048; 0xFFFF = sin dato */
	uint16_t hs_res[4];     /* shunt del puerto, como en el overlay */
} __packed;

/* ⚠️ CONTRATO CON LA PASARELA. `gateway.py` desempaqueta la carga (sin los 14
 * bytes de cabecera Ethernet) con:
 *
 *     MPS_FMT      = "!IHHII" + "h"*4 + "H"*4   -> 32 bytes
 *     MPS_FMT_V2   = "!HHiHH"                   -> 12 bytes
 *     MPS_FMT_V3   = "!Q"                       ->  8 bytes
 *
 * 14 + 32 + 12 + 8 = 66. Tocar la estructura sin tocar el parser daria un
 * fallo SILENCIOSO: tramas que decodifican y dan corrientes absurdas.
 *
 *     MPS_FMT_V4   = "!HHBB"  (vec_total, idx0, n, pad) -> 6 bytes
 *                    + MPS_VEC_POR_TRAMA * "!6sBB"      -> 8 cada uno
 */
BUILD_ASSERT(sizeof(struct mps_tele) == 66 + 6 + 8 * MPS_VEC_POR_TRAMA + 4 * 4,
	     "struct mps_tele desalineada con MPS_FMT de gateway.py");

/* Identidad estable de la placa, derivada del USN del MAX32690.
 *
 * ⚠️ NO se usa MXC_SYS_GetUSN(): arrastra MXC_FLC_UnlockInfoBlock y
 * MXC_CTB_Init -- driver de flash y motor criptografico -- que este HAL no
 * compila, y el enlazado falla con "undefined reference".
 *
 * Se lee el info block directamente, con la misma secuencia que ya usamos por
 * SWD para inspeccionar la CRK. SOLO LECTURA, y se RE-BLOQUEA SIEMPRE antes de
 * salir: ahi vive la CRK, y dejarlo abierto seria arriesgar que un fallo
 * posterior la corrompa y la placa no vuelva a arrancar.
 *
 * FNV-1a en vez de truncar: los USN de un mismo lote comparten prefijo y
 * truncar podria colisionar entre placas hermanas, que es justo el fallo que
 * este campo existe para evitar.
 *
 * Gemela de mfs_dev_id() en `field-switch/src/main.c` (rama mfs). */
#define MPS_USN_WORDS 4

static uint64_t mps_dev_id(void)
{
	volatile uint32_t *info = (volatile uint32_t *)MXC_INFO0_MEM_BASE;
	uint64_t h = 1469598103934665603ULL;   /* FNV-1a 64, offset basis */
	uint32_t w[MPS_USN_WORDS];
	int vacio = 1;

	MXC_FLC0->actrl = 0x1234;
	MXC_FLC0->actrl = 0x3a7f5ca3;
	MXC_FLC0->actrl = 0xa1e34f20;
	MXC_FLC0->actrl = 0x9608b2c1;

	for (int i = 0; i < MPS_USN_WORDS; i++) {
		w[i] = info[i];
	}

	MXC_FLC0->actrl = 0xDEADBEEF;          /* re-bloquear SIEMPRE */

	for (int i = 0; i < MPS_USN_WORDS; i++) {
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

volatile uint64_t g_dev_id;   /* identidad de esta placa (0 = sin USN) */

/* MAC localmente administrada, misma familia que el resto del banco
 * (la spe0 de la TPU es 02:00:00:AD:11:10).
 * ⚠️ Es la MISMA en todas las placas: por eso hace falta dev_id. */
static const uint8_t mps_mac[6] = { 0x02, 0x00, 0x00, 0xAD, 0x4C, 0x01 };

volatile int g_tele_rc;      /* ultimo retorno de SES_XmitFrame */
volatile unsigned g_tele_n;  /* tramas emitidas */
volatile unsigned short g_vec_total; /* tamano observado de la tabla dinamica */
static sesID_t g_ses_dev;            /* id del switch, para leer su tabla */

/* Vigilante de bloqueo del LTC4296 */
volatile unsigned short g_gcmd;      /* ultimo GCMD leido */
volatile unsigned short g_unlock_n;  /* veces que hubo que re-desbloquear */

/* Testigos del driver: el Vin que midio la ultima clasificacion, incluida la
 * del arranque en frio, que es la unica que importa para este fallo. */
extern volatile int      g_ltc_vin_mv;
extern volatile unsigned char  g_ltc_vin_ok;
extern volatile unsigned short g_ltc_disc_n;

static void mps_tele_send(const struct device *ltc)
{
	static uint32_t seq;
	static const enum ltc4296_port pp[4] = {
		LTC_PORT0, LTC_PORT1, LTC_PORT2, LTC_PORT3 };
	struct mps_tele f;
	SES_transmitFrameData_t tx;

	memset(&f, 0, sizeof(f));
	memset(f.dst, 0xFF, sizeof(f.dst));            /* difusion */
	memcpy(f.src, mps_mac, sizeof(f.src));
	f.ethertype = htons(MPS_ETHERTYPE);
	f.magic     = htonl(MPS_MAGIC);
	f.version   = htons(5);
	f.nports    = htons(4);
	seq++;
	f.seq       = htonl(seq);
	f.uptime_ms = htonl((uint32_t)k_uptime_get_32());

	for (int i = 0; i < 4; i++) {
		int ima = 0;
		uint16_t st = 0;

		/* ⚠️ El ADC de puerto solo tiene dato valido (bit NEW) en los
		 * puertos que entregan. Si falla se manda el centinela: un 0
		 * seria un dato falso perfectamente creible. */
		if (ltc4296_read_port_adc(ltc, pp[i], &ima) == 0) {
			if (ima >  32000) { ima =  32000; }
			if (ima < -32000) { ima = -32000; }
			f.iout_ma[i] = (int16_t)htons((uint16_t)(int16_t)ima);
		} else {
			f.iout_ma[i] = (int16_t)htons((uint16_t)MPS_I_NA);
		}

		if (ltc4296_read_port_status(ltc, pp[i], &st) != 0) { st = 0; }
		f.pxst[i] = htons(st);

		/* v5: la misma medida SIN convertir, para que la pasarela pueda
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
	f.dev_id  = sys_cpu_to_be64(g_dev_id);

	/* --- vecinos: un trozo de la tabla dinamica del switch --------------
	 * ⚠️ El indice de partida AVANZA en cada trama, para que a lo largo de
	 * varias se cubra la tabla entera sin engordar ninguna. */
	{
		static uint16_t vec_cursor;
		SES_dynTblEntry_t ent[MPS_VEC_POR_TRAMA];
		uint16_t validas = 0;
		int32_t rc;

		memset(ent, 0, sizeof(ent));
		rc = SES_MX_ReadDynamicTable(g_ses_dev, vec_cursor,
					     vec_cursor + MPS_VEC_POR_TRAMA - 1,
					     ent, &validas);
		if (rc != 0) {
			validas = 0;
		}
		if (validas > MPS_VEC_POR_TRAMA) {
			validas = MPS_VEC_POR_TRAMA;
		}
		f.vec_idx0  = htons(vec_cursor);
		f.vec_n     = (uint8_t)validas;
		f.vec_total = htons(g_vec_total);
		for (uint16_t i = 0; i < validas; i++) {
			memcpy(f.vec[i].mac, ent[i].macAddress, 6);
			f.vec[i].port = ent[i].portMap;
		}
		/* Si esta pasada devolvio menos de las pedidas, se acabo la tabla:
		 * se anota su tamano y se vuelve al principio. */
		if (validas < MPS_VEC_POR_TRAMA) {
			g_vec_total = vec_cursor + validas;
			vec_cursor = 0;
		} else {
			vec_cursor += validas;
		}
	}

	memset(&tx, 0, sizeof(tx));
	tx.frameType      = SES_standardFrame;
	tx.data_p         = &f;
	tx.byteCount      = sizeof(f);
	tx.ses.generateFcs   = 1;
	tx.ses.egressPortMap = 0xFF;   /* por todos: la TPU puede colgar de cualquiera */

	g_tele_rc = SES_XmitFrame(&tx);
	if (g_tele_rc == 0) { g_tele_n++; }
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
	/* phyConfig = {autoNegEnable, phyPullupCtrl, phyAddr, speed, duplex, crossover}
	 *
	 * phyPullupCtrl DEBE ser 1. Con 0 el SES no llega a identificar el PHY
	 * (PHYID lee 0x0000 en vez de 0x0283/0xBC81), no lo configura, y la
	 * autonegociacion queda deshabilitada (BMCR 0x0100, bit12=0): el puerto
	 * nunca enlaza y el LED del modulo no enciende. Con 1: PHYID 0x0283/0xBC81,
	 * BMCR 0x1100 (autoneg ON) y enlace correcto en los 4 puertos SPE.
	 *
	 * Fuente: configuracion de referencia de ADI para el field switch,
	 * portConfigurationFieldSwitch[] en example/src/SES_example_config.c de
	 * github.com/analogdevicesinc/windows-project-for-adinx310 (ADI pone
	 * phyPullupCtrl=1 en los seis puertos).
	 *
	 * NO volver a ponerlo en 0. Sintoma: puerto SPE presente que nunca linkea.
	 */
	const SES_portInit_t initializePorts_p[] = {
		/* Port 0: fixed 1 Gbps MAC-to-MAC RGMII link to the LAN7431.
		 * LAN7431 MAC_RGMII_ID=0x2: TXC delay enabled, RXC delay disabled.
		 * Add only the complementary ADIN6310 TX delay. Px_LINK is active low.
		 */
		{ 1, SES_rgmiiMode, { 0, 1, 0 }, 1, SES_phyUnmanaged, {true, 1, 0, SES_phySpeed1000, SES_phyDuplexModeFull, SES_autoMdix}},
		{ 1, SES_rmiiMode, { 0, 0, 0 }, 1, SES_phyADIN1100, {true, 1, 5, SES_phySpeed10, SES_phyDuplexModeFull, SES_autoMdix}},
		{ 1, SES_rmiiMode, { 0, 0, 0 }, 1, SES_phyADIN1100, {true, 1, 2, SES_phySpeed10, SES_phyDuplexModeFull, SES_autoMdix}},
		{ 1, SES_rmiiMode, { 0, 0, 0 }, 1, SES_phyADIN1100, {true, 1, 3, SES_phySpeed10, SES_phyDuplexModeFull, SES_autoMdix}},
		{ 1, SES_rmiiMode, { 0, 0, 0 }, 1, SES_phyADIN1100, {true, 1, 7, SES_phySpeed10, SES_phyDuplexModeFull, SES_autoMdix}},
		{ 1, SES_rmiiMode, { 0, 0, 0 }, 1, SES_phyADIN1300, {true, 1, 1, SES_phySpeed100, SES_phyDuplexModeFull, SES_autoMdix}}
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
	srand(k_cycle_get_32());
	mac_addr[3] = rand();
	mac_addr[4] = rand();
	mac_addr[5] = rand();

	/* Identidad de esta placa para la telemetria. Se lee UNA vez: toca el
	 * bloque de informacion de la flash y no hay motivo para repetirlo. */
	g_dev_id = mps_dev_id();

	ret = adin6310_enable_pse(ltc4296_dev, switch_op);
	g_pse_initret = (unsigned int)ret;
	{
		/* GCMD = 0x08, UNLOCK_KEY = 0x05. Si el SPI habla, la relectura
		 * de GCMD devuelve 0x05. Si devuelve 0xffff, el chip sigue mudo. */
		uint16_t v = 0;
		int io = 0;

		ltc4296_unlock(ltc4296_dev);
		ltc4296_reg_read(ltc4296_dev, 0x08, &v);
		g_pse_gcmd = v;

		v = 0;
		ltc4296_read_global_faults(ltc4296_dev, &v);
		g_pse_gflt = v;

		for (int p = 0; p < 4; p++) {
			uint16_t st = 0, ev = 0;

			io = 0;
			ltc4296_read_port_status(ltc4296_dev, (enum ltc4296_port)p, &st);
			ltc4296_read_port_events(ltc4296_dev, (enum ltc4296_port)p, &ev);
			ltc4296_read_port_adc(ltc4296_dev, (enum ltc4296_port)p, &io);
			g_pse_stat[p] = st;
			g_pse_evt[p] = ev;
			g_pse_iout[p] = io;
		}
	}
	if (ret){
		printf("Could not initialize %s\n", ltc4296_dev->name);
	}

	/* Registros globales, nunca mirados hasta ahora, y estado del UVLO. */
	{
		uint16_t v = 0;

		ltc4296_reg_read(ltc4296_dev, 0x06, &v); g_gcap = v;
		ltc4296_reg_read(ltc4296_dev, 0x09, &v); g_gcfg = v;
		ltc4296_reg_read(ltc4296_dev, 0x07, &v); g_giost = v;
		ltc4296_read_global_faults(ltc4296_dev, &v); g_gflt_pre = v;

		/* GFLT bit4 = UVLO_DIGITAL. Si esta latcheado del arranque se
		 * limpia; si vuelve a aparecer es que esta activo de verdad. */
		ltc4296_clear_global_faults(ltc4296_dev);
		ltc4296_clear_ckt_breaker(ltc4296_dev);
		k_msleep(50);
		ltc4296_read_global_faults(ltc4296_dev, &v); g_gflt_post = v;
	}

	/* ASERCION DE PROTECCIONES (auditoria 2026-07-23): garantizar TLIM
	 * (limite termico) ACTIVO y linea de baja NO enmascarada, sea cual sea el
	 * estado previo del chip. Guard: solo RMW si la lectura es valida (nunca
	 * escribir a partir de 0xffff con SPI degradado). */
	{
		uint16_t v = 0;

		ltc4296_reg_read(ltc4296_dev, 0x09, &v);
		if (v != 0xffff && (v & 0x0030) != 0) {
			/* limpiar TLIM_DISABLE (bit4) y MASK_LOWFAULT (bit5) */
			ltc4296_reg_write(ltc4296_dev, 0x09, (uint16_t)(v & ~0x0030));
		}
		ltc4296_reg_read(ltc4296_dev, 0x09, &g_gcfg); /* estado final, visible por SWD */
	}

	/* BLOQUE (2) FORZADO ELIMINADO (config segura Clase 13). El encendido
	 * lo hace ltc4296_probe() por do_spoe_sccp(): deteccion+clasificacion
	 * SCCP y potencia SOLO si negocia un PD valido, con TODAS las
	 * protecciones por defecto (TLIM activo, foldback, soft-start,
	 * TINRUSH=56.2ms). NO se fuerza salida ni se desactiva TLIM. */

	/* Sin intervencion manual: con la clase SPoE correcta, ltc4296_probe()
	 * ya ejecuta do_spoe_sccp() (deteccion + clasificacion + encendido).
	 * Aqui solo se observa el resultado. */
	{
		static const enum ltc4296_port pp[4] = {
			LTC_PORT0, LTC_PORT1, LTC_PORT2, LTC_PORT3 };
		uint16_t v = 0;

		k_msleep(2000);   /* dar tiempo a que SCCP termine */

		for (int q = 0; q < 4; q++) {
			uint16_t st = 0, ev = 0, c0 = 0, c1 = 0;
			int io = 0;

			ltc4296_read_port_status(ltc4296_dev, pp[q], &st);
			ltc4296_read_port_events(ltc4296_dev, pp[q], &ev);
			ltc4296_read_port_adc(ltc4296_dev, pp[q], &io);
			/* leer de vuelta la config REAL del puerto, sin suponerla */
			ltc4296_reg_read(ltc4296_dev, (uint8_t)(0x13 + q * 0x10), &c0);
			ltc4296_reg_read(ltc4296_dev, (uint8_t)(0x14 + q * 0x10), &c1);
			g_pse2_stat[q] = st;
			g_pse2_evt[q] = ev;
			g_pse2_iout[q] = io;
			g_cfg0[q] = c0;
			g_cfg1[q] = c1;
			g_pse2_try[q] = 0;
		}
		ltc4296_read_global_faults(ltc4296_dev, &v);
		g_pse2_gflt = v;
	}

	/* PASO LADO BAJO (peticion Mayker): tras una negociacion exitosa, activar el
	 * retorno (LGATE/Q18). En el LTC4296-1 el LGATE es COMPARTIDO (un unico FET de
	 * retorno) y engancha al entrar en power-up; no hay bit de firmware para
	 * "encender LGATE". Aqui: si algun puerto ya NEGOCIO y entrega (PxST bits 2:0
	 * = 2 = DELIVERING), se RE-ARMA el disyuntor de baja (sin enmascararlo) para
	 * que Q18 enganche, y se VERIFICA que la baja no dispara. NO fuerza potencia. */
	{
		static const enum ltc4296_port pp[4] = {
			LTC_PORT0, LTC_PORT1, LTC_PORT2, LTC_PORT3 };
		uint16_t st = 0, ev = 0, v = 0;
		int any = 0;

		/* 1) que puertos negociaron: PxST field (bits 2:0) == 2 (DELIVERING) */
		for (int q = 0; q < 4; q++) {
			ltc4296_read_port_status(ltc4296_dev, pp[q], &st);
			g_lg_st[q] = st;
			g_lg_deliver[q] = ((st & 0x0007) == 0x0002) ? 1 : 0;
			if (g_lg_deliver[q])
				any = 1;
		}
		g_lg_any = any;

		/* 2) si hubo negociacion, RE-ARMAR el disyuntor de baja (LGATE/Q18).
		 * NO se toca MASK_LOWFAULT: la baja queda visible y protegiendo. */
		if (any) {
			g_lg_rearm_rc = ltc4296_clear_ckt_breaker(ltc4296_dev);
			k_msleep(50);
		} else {
			g_lg_rearm_rc = -1;   /* nadie nego -> no se re-arma (correcto) */
		}

		/* 3) VERIFICAR: LOW_CKT_BRK_FAULT (GFLTEV=0x02, bit0) y PxEV por puerto
		 * (bit0 LSNS_REVERSE, bit1 LSNS_FORWARD). Baja sin disparar = LGATE ok. */
		ltc4296_reg_read(ltc4296_dev, 0x02, &v);
		g_lg_gfltev = v;
		for (int q = 0; q < 4; q++) {
			ltc4296_read_port_events(ltc4296_dev, pp[q], &ev);
			g_lg_pxev[q] = ev;
		}
	}

	/* VIN y VOUT DESPUES. VOUT ~0 en todos = no sale tension del PSE;
	 * VOUT alto solo en el puerto con carga = el problema es la carga. */
	{
		static const enum ltc4296_port qq[4] = {
			LTC_PORT0, LTC_PORT1, LTC_PORT2, LTC_PORT3 };
		int mv = 0;

		g_vrc[2] = ltc4296_set_gadc_vin(ltc4296_dev);
		k_msleep(50);
		g_vrc[3] = ltc4296_read_gadc(ltc4296_dev, &mv);
		g_vin_post = mv;
		ltc4296_disable_gadc(ltc4296_dev);

		for (int q = 0; q < 4; q++) {
			mv = 0;
			ltc4296_set_gadc_vout(ltc4296_dev, qq[q]);
			k_msleep(50);
			ltc4296_read_gadc(ltc4296_dev, &mv);
			g_vout_mv[q] = mv;
			ltc4296_disable_gadc(ltc4296_dev);
		}
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

	/* Para leer la tabla dinamica desde la telemetria (los vecinos por
	 * puerto, base de la jerarquia). Igual que en el field switch. */
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

	{
		/* El ADIN1300 del RJ45 (puerto 5) arranca en RGMII. Hay que habilitarle
		 * RMII poniendo GE_RMII_CFG (MMD 0x1E, reg 0xFF24) bit0 = RMII_EN y
		 * reiniciar su autonegociacion. Sin esto el puerto 5 no enlaza.
		 * Se escribe por las dos vias: la codificacion C45 de SES no siempre
		 * alcanza ese registro vendor.
		 */
		uint16_t v;

		SES_ReadPhyReg(SES_macPort5, 0x1EFF24, &v);
		SES_WritePhyReg(SES_macPort5, 0x1EFF24, v | 0x0001);
		k_msleep(50);

		rj_mmd_wr(0x1E, 0xFF24, rj_mmd_rd(0x1E, 0xFF24) | 0x0001);
		k_msleep(50);

		/* BMCR: AN_EN | AN_RESTART */
		SES_ReadPhyReg(SES_macPort5, 0x0000, &v);
		SES_WritePhyReg(SES_macPort5, 0x0000, v | 0x1200);
		k_msleep(2000);
	}
	unsigned int lg_tick = 0;

	unsigned int tele_tick = 0;

	while (1) {
		k_sleep(K_MSEC(1000));

		if (++tele_tick >= MPS_TELE_TICKS) {
			tele_tick = 0;
			mps_tele_send(ltc4296_dev);
		}

		g_link[0] = SES_GetLinkState(SES_macPort0);
		g_link[1] = SES_GetLinkState(SES_macPort1);
		g_link[2] = SES_GetLinkState(SES_macPort2);
		g_link[3] = SES_GetLinkState(SES_macPort3);
		g_link[4] = SES_GetLinkState(SES_macPort4);
		g_link[5] = SES_GetLinkState(SES_macPort5);

		/* Reintento SPoE (hot-plug): si un puerto PSE aun NO entrega (p.ej. los
		 * 50V/PD se conectaron despues del arranque), re-negociar por SCCP y,
		 * tras negociar, re-armar/verificar el lado bajo (LGATE). Cada ~5 s.
		 * Los puertos que YA entregan se saltan (no se les molesta). */
		if (++lg_tick >= 5) {
			static const enum ltc4296_port pp[4] = {
				LTC_PORT0, LTC_PORT1, LTC_PORT2, LTC_PORT3 };
			struct ltc4296_vi vi;
			uint16_t st = 0, ev = 0, v = 0;
			int any = 0;

			lg_tick = 0;

			/* ⚠️ VIGILANTE DE BLOQUEO — tiene que ir ANTES de los
			 * reintentos de abajo. El LTC4296 se re-bloquea solo y un chip
			 * bloqueado IGNORA LAS ESCRITURAS EN SILENCIO, asi que sin esto
			 * ltc4296_retry_spoe_sccp() escribiria al vacio indefinidamente
			 * y el puerto no volveria a negociar hasta un reset. probe()
			 * desbloquea una sola vez, al arrancar.
			 *
			 * NO se llama a ltc4296_chk_global_events() (la recuperacion del
			 * fabricante, sin un solo llamante en todo el arbol): cuando ve
			 * el chip bloqueado hace ltc4296_reset(), que TIRARIA la entrega
			 * de los puertos vivos. Reescribir la llave es idempotente. */
			{
				uint16_t gc = 0;

				if (ltc4296_reg_read(ltc4296_dev, 0x08, &gc) == 0) {
					if ((gc & 0x05) != 0x05) {
						ltc4296_unlock(ltc4296_dev);
						g_unlock_n++;
						/* releer: si sigue bloqueado, el problema es
						 * mas gordo y hay que verlo en el panel */
						ltc4296_reg_read(ltc4296_dev, 0x08, &gc);
					}
					g_gcmd = gc;
				}
			}

			/* re-negociar SOLO los puertos que aun no entregan (DELIVERING=2) */
			for (int q = 0; q < 4; q++) {
				ltc4296_read_port_status(ltc4296_dev, pp[q], &st);
				if ((st & 0x0007) != 0x0002)
					g_retry_rc[q] =
						ltc4296_retry_spoe_sccp(ltc4296_dev, pp[q], &vi);
			}

			/* deteccion de entrega + re-arme/verificacion del lado bajo */
			for (int q = 0; q < 4; q++) {
				ltc4296_read_port_status(ltc4296_dev, pp[q], &st);
				g_lg_st[q] = st;
				g_lg_deliver[q] = ((st & 0x0007) == 0x0002) ? 1 : 0;
				if (g_lg_deliver[q])
					any = 1;
			}
			g_lg_any = any;
			if (any) {
				g_lg_rearm_rc = ltc4296_clear_ckt_breaker(ltc4296_dev);
				k_msleep(50);
			}
			ltc4296_reg_read(ltc4296_dev, 0x02, &v);
			g_lg_gfltev = v;
			for (int q = 0; q < 4; q++) {
				ltc4296_read_port_events(ltc4296_dev, pp[q], &ev);
				g_lg_pxev[q] = ev;
			}
		}
	}

	return 0;
}
