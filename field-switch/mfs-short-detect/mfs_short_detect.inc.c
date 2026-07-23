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
#define MFS_PSE_PORTS       4       /* puertos PSE configurados (LTC0..3).
                                     * Subir a 5 si se habilita LTC_PORT4
                                     * (slot Port 6): ver README. */
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
volatile uint16_t g_short_mask;              /* bit p = LTC_PORTp en corto */
volatile int      g_short_vout[MFS_PSE_PORTS];

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
static uint16_t mfs_scan_shorts(const struct device *dev)
{
	uint16_t mask = 0;

	for (int p = 0; p < MFS_PSE_PORTS; p++) {
		enum ltc4296_pse_status pwr = LTC_PSE_STATUS_UNKNOWN;

		/* no molestar a un puerto entregando a un PD real */
		if (ltc4296_is_port_deliver_pwr(dev, (enum ltc4296_port)p, &pwr) == 0 &&
		    pwr == LTC_PSE_STATUS_DELIVERING) {
			g_short_vout[p] = -1;   /* marca "entregando", no sondeado */
			continue;
		}

		int v = mfs_probe_port_vout(dev, (enum ltc4296_port)p);
		g_short_vout[p] = v;

		/* corto = Vout bajo Y positivo. Un valor <0 o muy grande
		 * (p.ej. +/-71000 con SPI/ADC mudo) NO es corto: se ignora
		 * para evitar falsos positivos. */
		if (v >= 0 && v < MFS_SHORT_VOUT_MV)
			mask |= (1u << p);
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
