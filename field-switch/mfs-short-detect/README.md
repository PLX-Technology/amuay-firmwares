# Detección de cortos en módulos PSM/PDM con indicación por LED

Feature solicitada por Mayker (2026-07-23). Permite diagnosticar en campo
—sin debugger ni óhmetro— cuál slot tiene un módulo PSM/PDM con el par de
potencia en corto (el modo de fallo que encontramos: `C10` perforado, ver
[`../PDM-SPE-NOTES.md`](../PDM-SPE-NOTES.md)).

## Qué hace

Al arrancar (y re-escaneado cada ~30 s) el firmware prueba cada puerto PSE
en **modo clasificación** (corriente de µA, **nunca entrega potencia**) y lee
el Vout de sondeo con el ADC interno del LTC4296:

| Vout de sondeo | Veredicto |
|---|---|
| ~5100 mV | slot vacío (sondeo contra abierto) |
| 4050–4550 mV → DELIVERING | PD sano |
| **< 2000 mV (~70 mV)** | **módulo en CORTO** |

Por cada slot en corto, parpadea el LED de fallo **N veces = número de slot
en la serigrafía**, con una pausa larga entre slots. Sin cortos → LED apagado.

Ejemplo: corto en el slot **Port 4** → 4 parpadeos, pausa. Cortos en Port 3 y
Port 5 → 3 parpadeos, pausa larga, 5 parpadeos, pausa de ciclo, y repite.

## Mapeo slot ↔ puerto LTC (confirmado midiendo el PSM en slot Port 4)

```
Slot Port 1 = UPLINK (el PDM recibe PoDL; NO es salida PSE)
Slot Port 2 = LTC_PORT0        Slot Port 4 = LTC_PORT2
Slot Port 3 = LTC_PORT1        Slot Port 5 = LTC_PORT3
Slot Port 6 = LTC_PORT4  (ver "Puerto 6" abajo)
=> slot serigrafía = LTC_port + 2
```

## Integración en `main.c` (sample `adin6310_mfs`)

El código está en [`mfs_short_detect.inc.c`](mfs_short_detect.inc.c) (funciones
autocontenidas, toda la API es pública). Tres puntos de inserción:

1. **Globales + funciones** — pegar el contenido de `mfs_short_detect.inc.c`
   antes de `int main(void)`.

2. **En `main()`, tras inicializar el PSE** (después de
   `ltc4296_config = ltc4296_dev->config;`):
   ```c
   /* LED de fallo = serigrafía "LED2" (verde, gpio0.11, alias led1).
    * Alternativa roja: DT_ALIAS(led2) (silk "LED3", gpio0.12). */
   struct gpio_dt_spec fault_led = GPIO_DT_SPEC_GET(DT_ALIAS(led1), gpios);
   gpio_pin_configure_dt(&fault_led, GPIO_OUTPUT_INACTIVE);
   mfs_scan_shorts(ltc4296_dev);   /* escaneo inicial */
   ```

3. **En el bucle `while (1)` principal** (junto al `k_sleep(K_MSEC(1000))`):
   ```c
   static int rescan = 0;
   if (++rescan >= MFS_RESCAN_EVERY) { rescan = 0; mfs_scan_shorts(ltc4296_dev); }
   if (g_short_mask)
       mfs_blink_shorts(&fault_led, g_short_mask);  /* bloquea 1 ciclo */
   else
       gpio_pin_set_dt(&fault_led, 0);
   ```

## Verificación por SWD (mientras no haya consola)

Globales de diagnóstico:
- `g_short_mask` — bit p a 1 = LTC_PORTp en corto.
- `g_short_vout[p]` — Vout de sondeo por puerto (mV); `-1` = estaba entregando.

Contrastar con los scripts de [`../../tools/ltc4296-swd/`](../../tools/ltc4296-swd/):
el mismo Vout que miden a mano debe coincidir con `g_short_vout[]`.

## LED de fallo = P0.13 (CONFIRMADO en placa)

⚠️ Los alias de LED del board dts estándar (`led0/1/2` → gpio2.1/gpio0.11/
gpio0.12) **NO están poblados/visibles en la placa MFS** (probado: ninguno
enciende al forzarlos por SWD). El LED de usuario real del field switch está en
**P0.13** (dato de Mayker, verificado forzando `gpio0.13` alto → enciende).

Por eso el `app.overlay` define un nodo propio y la feature lo usa:
```
/ {
	mfsleds {
		compatible = "gpio-leds";
		mfs_fault_led: mfs_fault_led {
			gpios = <&gpio0 13 0>;   /* P0.13, activo alto */
			label = "FAULT_P013";
		};
	};
};
```
En `main.c`: `GPIO_DT_SPEC_GET(DT_NODELABEL(mfs_fault_led), gpios)`.
Overlay de referencia: [`app.overlay.reference`](app.overlay.reference).

**Verificado en placa (2026-07-23):** PSM en corto en slot Port 4 → el LED P0.13
**parpadea 4 veces** (= nº de slot), pausa, repite. `g_short_mask=0x04`,
`g_short_vout[2]=70 mV`. ✓

## Puerto 6 (5º puerto PSE, LTC_PORT4)

El slot **Port 6** existe en hardware (`P4_PWR`, LTC_PORT4) pero el firmware
**no lo instancia**: el driver solo define `port0..port3` y el overlay solo
tiene esos 4 nodos. Para cubrirlo también:

1. Añadir nodo `port4` al overlay con `adi,power-class` y `adi,hs-resistor`.
2. Añadir `.port_config[4] = LTC4296_PORT_INIT(inst, port4)` al macro
   `LTC4296_DEFINE` del driver.
3. Verificar en el board dts que los GPIO `sccpi/sccpo` del puerto 4 están
   ruteados (los otros usan `gpio2.14..22`).
4. Subir `MFS_PSE_PORTS` a `5` en `mfs_short_detect.inc.c`.

## ✅ Estado: EN VIVO Y FUNCIONANDO (2026-07-23)

Flasheado y **probado en placa** en el firmware definitivo
`../prebuilt/mfs_clean_class11.sbin`:
- Arranca (build fresco, secure boot OK) — ver [`../BOOT-TROUBLESHOOTING.md`](../BOOT-TROUBLESHOOTING.md).
- La detección corre al arrancar: `g_short_mask=0` (sin cortos), sondeo de
  `5178 mV` por puerto (abierto). LED apagado. ✓
- Modo **Clase 11** en el overlay (seguro: sin entrega ciega).
- Fuente de referencia completa: [`main.c.reference`](main.c.reference) (init
  limpia del switch SPE + este módulo, SIN el bloque RJ45 que crasheaba).

El bloqueo de "builds frescos no arrancan" **quedó resuelto**: era el bloque de
sondeo RJ45 del `macPort5` (hardware inexistente en el field switch), no el
secure boot. El firmware es libremente modificable.

### ⚠️ Al compilar desde fuente (cuando el bloqueo se resuelva)

1. **`CONFIG_FLASH_LOAD_OFFSET=0x100`** — el `prj.conf` del sample lo tiene
   comentado, así que un build pristine sale con offset **0** (mal: vectores
   en el sitio equivocado; sospechoso nº1 del no-arranque). Forzarlo en
   `prj.conf`.
2. **Overlay a Clase 11** — para la entrega normal, poner los 4 (o 5) puertos
   a `LTC4296_PSE_SCCP_CLASS_11` en el `app.overlay` (el cambio de clase que
   hoy va por binpatch de datos). *La detección de cortos funciona igual con
   cualquier clase* porque hace su propio sondeo de clasificación.
3. Firmar con el `__start` real del build (`grep __start zephyr.map`).
