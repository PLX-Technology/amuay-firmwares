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
K_THREAD_STACK_DEFINE(stack_area, 2000);
K_SEM_DEFINE(reader_thread_sem, 0, 1);
K_MUTEX_DEFINE(spi_mutex);
volatile int g_link[6];
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
		if (ret)
			return;
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
	 * nunca enlaza y el LED del modulo no enciende.
	 *
	 * Diagnosticado y verificado en banco en el power switch (mismo SDK y
	 * mismo sintoma). Fuente del valor: configuracion de referencia de ADI
	 * para el field switch, portConfigurationFieldSwitch[] en
	 * example/src/SES_example_config.c de
	 * github.com/analogdevicesinc/windows-project-for-adinx310, que pone
	 * phyPullupCtrl=1 en los seis puertos.
	 *
	 * NO volver a ponerlo en 0. Sintoma: puerto SPE presente que nunca linkea.
	 */
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

	/* TEST-NORESET: P1.8 no se toca (diagnostico reset loop field switch) */
	(void)reset_gpio;

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

	k_sleep(K_MSEC(1));
	srand(k_cycle_get_32());
	mac_addr[3] = rand();
	mac_addr[4] = rand();
	mac_addr[5] = rand();

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

	ret = SES_AddDevice(iface, mac_addr, &dev_id);
	if (ret) {
		printf("SES_AddDevice() error %d\n", ret);
		return ret;
	}

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
		SES_mac_t spe[4] = { SES_macPort1, SES_macPort2, SES_macPort3, SES_macPort4 };
		uint16_t v;
		for (int k = 0; k < 4; k++) {
			g_phyret[k] = SES_ReadPhyReg(spe[k], 0x0002, &v); g_phyid1[k] = v;
			SES_ReadPhyReg(spe[k], 0x0003, &v); g_phyid2[k] = v;
			SES_ReadPhyReg(spe[k], 0x1E8C82, &v); g_ledctrl[k] = v;
		}
		SES_ReadPhyReg(SES_macPort5, 0x0002, &v); g_ethid1 = v;
		SES_ReadPhyReg(SES_macPort5, 0x0003, &v); g_ethid2 = v;
	}
	{
		static SES_portInit_t kp[6];
		uint16_t id1, led;
		for (int i = 0; i < 6; i++) kp[i] = initializePorts_p[i];
		for (int a = 0; a < 32; a++) {
			kp[5].phyConfig.phyAddr = a;
			SES_MX_InitializePorts(dev_id, 6, kp);
			k_msleep(15);
			SES_ReadPhyReg(SES_macPort5, 0x010002, &id1);
			SES_ReadPhyReg(SES_macPort5, 0x1E8C82, &led);
			g_scan[a*2] = id1; g_scan[a*2+1] = led;
		}
		SES_MX_InitializePorts(dev_id, 6, initializePorts_p);
	}
	{
		uint16_t v;
		/* --- ANTES: estado del ADIN1300 --- */
		SES_ReadPhyReg(SES_macPort5, 0x0000, &v); g_rj[0] = v;  /* BMCR */
		SES_ReadPhyReg(SES_macPort5, 0x0001, &v); g_rj[1] = v;  /* BMSR */
		SES_ReadPhyReg(SES_macPort5, 0x0004, &v); g_rj[2] = v;  /* ANAR  */
		SES_ReadPhyReg(SES_macPort5, 0x0005, &v); g_rj[3] = v;  /* ANLPAR */
		g_rj[4] = rj_mmd_rd(0x1E, 0xFF24);   /* GE_RMII_CFG  bit0=RMII_EN */
		g_rj[5] = rj_mmd_rd(0x1E, 0xFF23);   /* GE_RGMII_CFG */

		/* --- FIX: forzar RMII. Via 1: codificacion C45 de SES (0xDDRRRR) --- */
		SES_ReadPhyReg(SES_macPort5, 0x1EFF24, &v); g_rj[13] = v;
		g_rj[14] = (unsigned short)SES_WritePhyReg(SES_macPort5, 0x1EFF24, v | 0x0001);
		k_msleep(50);
		SES_ReadPhyReg(SES_macPort5, 0x1EFF24, &v); g_rj[15] = v;
		/* --- Via 2: indirecto manual 0x0D/0x0E --- */
		rj_mmd_wr(0x1E, 0xFF24, g_rj[4] | 0x0001);
		k_msleep(50);
		g_rj[6] = rj_mmd_rd(0x1E, 0xFF24);   /* releer: bit0 debe quedar en 1 */

		/* --- reiniciar autoneg (BMCR: AN_EN|AN_RESTART) --- */
		SES_ReadPhyReg(SES_macPort5, 0x0000, &v);
		SES_WritePhyReg(SES_macPort5, 0x0000, v | 0x1200);
		k_msleep(2000);
		SES_ReadPhyReg(SES_macPort5, 0x0000, &v); g_rj[7] = v;  /* BMCR despues */
		SES_ReadPhyReg(SES_macPort5, 0x0001, &v); g_rj[8] = v;  /* BMSR despues */
		SES_ReadPhyReg(SES_macPort5, 0x0005, &v); g_rj[9] = v;  /* ANLPAR despues */
	}
	{
		static SES_portInit_t kp5[6];
		uint8_t addrs[2] = { 2, 4 };
		uint16_t v;
		int idx = 0;

		for (int i = 0; i < 6; i++) { kp5[i] = initializePorts_p[i]; }
		for (int a = 0; a < 2; a++) {
			kp5[5].phyConfig.phyAddr = addrs[a];
			SES_MX_InitializePorts(dev_id, 6, kp5);
			k_msleep(30);
			SES_ReadPhyReg(SES_macPort5, 0x010002, &v); g_p4[idx++] = v;  /* PHYID1 */
			SES_ReadPhyReg(SES_macPort5, 0x010003, &v); g_p4[idx++] = v;  /* PHYID2 */
			SES_ReadPhyReg(SES_macPort5, 0x0108F7, &v); g_p4[idx++] = v;  /* B10L_STAT bit0=LINK */
			SES_ReadPhyReg(SES_macPort5, 0x070201, &v); g_p4[idx++] = v;  /* AN_STAT bit5=AN ok */
			SES_ReadPhyReg(SES_macPort5, 0x010834, &v); g_p4[idx++] = v;  /* PMA_CTRL */
			SES_ReadPhyReg(SES_macPort5, 0x1E8C82, &v); g_p4[idx++] = v;  /* LEDCTRL */
		}
		/* RESTAURAR port5: dejarlo mal-configurado CRASHEA la app */
		SES_MX_InitializePorts(dev_id, 6, initializePorts_p);
	}
	{
		static SES_portInit_t km[6];
		uint16_t v;

		for (int i = 0; i < 6; i++) { km[i] = initializePorts_p[i]; }
		/* port5 como ADIN1300 SOLO para escanear: leer via un puerto
		 * ADIN1100 da 0x0000. Se restaura al final. */
		km[5].phyType = SES_phyADIN1300;
		for (int a = 0; a < 32; a++) {
			km[5].phyConfig.phyAddr = a;
			SES_MX_InitializePorts(dev_id, 6, km);
			k_msleep(20);
			/* clause 45: MMD1 regs 2/3 -> ADIN1100 (modulos SPE) */
			v = 0; SES_ReadPhyReg(SES_macPort5, 0x010002, &v); g_map[a*4+0] = v;
			v = 0; SES_ReadPhyReg(SES_macPort5, 0x010003, &v); g_map[a*4+1] = v;
			/* clause 22: regs 2/3 -> ADIN1300 (RJ45) */
			v = 0; SES_ReadPhyReg(SES_macPort5, 0x0002, &v);   g_map[a*4+2] = v;
			v = 0; SES_ReadPhyReg(SES_macPort5, 0x0003, &v);   g_map[a*4+3] = v;
		}
		/* RESTAURAR port5: dejarlo mal-configurado CRASHEA la app */
		SES_MX_InitializePorts(dev_id, 6, initializePorts_p);
	}
	while (1) {
		k_sleep(K_MSEC(1000));
		g_link[0] = SES_GetLinkState(SES_macPort0);
		g_link[1] = SES_GetLinkState(SES_macPort1);
		g_link[2] = SES_GetLinkState(SES_macPort2);
		g_link[3] = SES_GetLinkState(SES_macPort3);
		g_link[4] = SES_GetLinkState(SES_macPort4);
		g_link[5] = SES_GetLinkState(SES_macPort5);
		{
			uint16_t v;
			SES_ReadPhyReg(SES_macPort5, 0x0001, &v); g_rj[10] = v;  /* BMSR vivo */
			SES_ReadPhyReg(SES_macPort5, 0x0005, &v); g_rj[11] = v;  /* ANLPAR vivo */
			g_rj[12] = rj_mmd_rd(0x1E, 0xFF24);
		}
		{
			SES_statistic_t s0, s5, s3;
			SES_GetStatistics(SES_macPort0, &s0, sizeof(s0));
			SES_GetStatistics(SES_macPort5, &s5, sizeof(s5));
			SES_GetStatistics(SES_macPort3, &s3, sizeof(s3));
			g_st[0]=s0.rxByte; g_st[1]=s0.rxBroadcast; g_st[2]=s0.rxFcsError; g_st[3]=s0.txByte; g_st[4]=s0.txBroadcast;
			g_st[5]=s5.rxByte; g_st[6]=s5.rxBroadcast; g_st[7]=s5.rxFcsError; g_st[8]=s5.txByte; g_st[9]=s5.txBroadcast;
			g_st3[0]=s3.rxByte; g_st3[1]=s3.rxBroadcast; g_st3[2]=s3.rxFcsError; g_st3[3]=s3.txByte; g_st3[4]=s3.txBroadcast;
		}
		{
			SES_statistic_t sp;
			SES_mac_t pl[4] = { SES_macPort1, SES_macPort2, SES_macPort3, SES_macPort4 };
			for (int q = 0; q < 4; q++) {
				SES_GetStatistics(pl[q], &sp, sizeof(sp));
				g_rx[q] = sp.rxByte;
				g_tx[q] = sp.txByte;
			}
		}
		{
			SES_mac_t pa[4] = { SES_macPort1, SES_macPort2, SES_macPort3, SES_macPort4 };
			/* devad<<16 | reg  --  ADIN1100 clause 45 */
			uint32_t rg[6] = { 0x070200, 0x070201, 0x010834, 0x0108F7, 0x030001, 0x010001 };
			uint16_t rv;
			for (int q = 0; q < 4; q++)
				for (int w = 0; w < 6; w++) {
					rv = 0;
					SES_ReadPhyReg(pa[q], rg[w], &rv);
					g_an[q][w] = rv;
				}
		}
		{
			uint8_t lp;
			g_probe[0] = SES_GetLinkPartnerAutoNegStatus(SES_macPort1, &lp); g_probe[1]=lp;
			g_probe[2] = SES_GetLinkPartnerAutoNegStatus(SES_macPort2, &lp); g_probe[3]=lp;
			g_probe[4] = SES_GetLinkPartnerAutoNegStatus(SES_macPort3, &lp); g_probe[5]=lp;
			g_probe[6] = SES_GetLinkPartnerAutoNegStatus(SES_macPort4, &lp); g_probe[7]=lp;
		}
	}

	return 0;
}
