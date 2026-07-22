# Config SEGURA del LTC4296 (power switch) — Clase 13 @ 50 V

> **Propuesta para revisar con Mayker. NO flasheado.** Reemplaza el bloque de
> encendido "de banco" que corría con las protecciones desactivadas (posible
> factor en la falla del LTC4296 anterior, junto a VIN/CPO).

## Por qué el bloque anterior era peligroso

El firmware que corría encendía los 4 puertos a 54 V **forzando la salida**
(`SIG/PREBIAS_OVERRIDE_GOOD`, se salta detección/clasificación) y con:
`TLIM_DISABLE` (sin límite térmico), `TINRUSH=forever` (sin timeout de inrush),
`TMFVDO/TOFF_TIMER_DISABLE` (sin auto-apagado por fallo). Solo quedaban
soft-start y foldback. Con el fallo conocido de la línea de baja, eso es la
receta para autodestrucción por sobre-disipación/sobrecorriente.

## La solución: usar el camino del propio driver

El driver `drivers/sensor/ltc4296` **ya entrega potencia de forma protegida**:

- `ltc4296_set_port_pwr()` → `PxCFG0 = SW_EN|POWER_AVAILABLE|PSE_READY|SET_CLASSIFICATION|END_CLASSIFICATION`, **sin** deshabilitar ninguna protección (TLIM, foldback, soft-start, TMFVDO, TOFF quedan **activos**).
- `ltc4296_port_prebias()` → `TINRUSH = 56.2 ms` (finito, no forever).
- `ltc4296_retry_spoe_sccp(dev, port, &vi)` → clasificación SCCP + entrega, leyendo la **clase del devicetree** y **validando Vin en rango** (50-58 V para clase 13) antes de energizar.

## Cambio 1 — overlay: Clase 13 (50-58 V)

En `app.overlay` del power switch, cambiar los 4 puertos de `LTC4296_PSE` (=APL
9.6-15 V, incorrecto para 50 V) a **`LTC4296_PSE_SCCP_CLASS_13`**:

```dts
&ltc4296 {
    port0 { adi,power-class = <LTC4296_PSE_SCCP_CLASS_13>; adi,hs-resistor = <250>; };
    port1 { adi,power-class = <LTC4296_PSE_SCCP_CLASS_13>; adi,hs-resistor = <250>; };
    port2 { adi,power-class = <LTC4296_PSE_SCCP_CLASS_13>; adi,hs-resistor = <250>; };
    port3 { adi,power-class = <LTC4296_PSE_SCCP_CLASS_13>; adi,hs-resistor = <250>; };
};
```
(`LTC4296_PSE_SCCP_CLASS_13 = 5`, de `dt-bindings/sensor/ltc4296.h`.)
Clase 13 = **231 mA máx**, la más suave del grupo 50-58 V (vs 600/1579 mA de
clase 14/15).

## Cambio 2 — main.c: reemplazar el bloque forzado por el camino seguro

Sustituir el bloque `(2) SOLUCION ...` (líneas ~655-687, el que hace
`ltc4296_reg_write(...,0x09,...|0x0010)` y `CFG0=0x01E1`/`CFG1=0x013D` a mano)
por:

**Criterio (Mayker):** NO forzar los 4 PSM a entregar en simultáneo. Cada
puerto entrega potencia **solo si hay una negociación correcta** (un PD real que
negoció al otro lado). Los 4 pueden quedar activos entregando a la vez, **siempre
que cada uno haya negociado**; un puerto sin PD negociado queda apagado. Esto es
exactamente lo que hace `ltc4296_retry_spoe_sccp`: clasifica por SCCP y energiza
**iff** encuentra un PD válido y compatible; si no, deja el puerto sin potencia.

```c
/* BLOQUE SEGURO — cada puerto entrega SOLO si SU negociacion SCCP tiene exito
 * (Mayker: nada de forzar; entrega solo con un PD negociado al otro lado). Los 4
 * pueden quedar activos a la vez SIEMPRE que cada uno haya negociado; un puerto
 * sin PD queda apagado. Protecciones por defecto (TLIM, foldback, soft-start,
 * TINRUSH=56.2ms). Clase 13: el driver valida Vin (50-58V) antes de entregar.
 * Cada puerto es INDEPENDIENTE: que uno no negocie o falle no afecta a los otros.
 * Encendido escalonado solo para no solapar el inrush. */
{
    static const enum ltc4296_port pp[4] = {
        LTC_PORT0, LTC_PORT1, LTC_PORT2, LTC_PORT3 };
    struct ltc4296_vi vi;

    /* Limpiar faults latcheados del arranque. NO tocar TLIM (queda activo). */
    ltc4296_clear_global_faults(ltc4296_dev);
    ltc4296_clear_ckt_breaker(ltc4296_dev);
    for (int q = 0; q < 4; q++)
        ltc4296_port_disable(ltc4296_dev, pp[q]);
    k_msleep(200);

    for (int q = 0; q < 4; q++) {
        /* Negocia (SCCP) y entrega SOLO si hay PD valido en ESTE puerto.
         * Sin PD -> el driver deja el puerto apagado (no fuerza nada).
         * Independiente por puerto: no se aborta el resto si este falla. */
        g_fx_ev[q] = ltc4296_retry_spoe_sccp(ltc4296_dev, pp[q], &vi);

        k_msleep(300);   /* escalonar: no solapar el inrush del siguiente */
        ltc4296_read_port_status(ltc4296_dev, pp[q], &g_fx_st[q][0]);
        ltc4296_read_port_adc(ltc4296_dev, pp[q], &g_fx_iout[q][0]);
    }
    /* Resultado: cada puerto quedo entregando IFF negocio un PD; ninguno forzado.
     * Reintentar periodicamente en el bucle principal para PDs que se conecten
     * despues (hot-plug): volver a llamar retry_spoe_sccp por puerto no-activo. */
}
```

## Qué queda protegido (vs el bloque viejo)

| Protección | Bloque viejo | Bloque seguro |
|---|---|---|
| Límite térmico (TLIM) | ❌ desactivado | ✅ activo |
| Foldback de corriente | ✅ | ✅ |
| Soft-start | ✅ | ✅ |
| Timeout de inrush (TINRUSH) | ❌ forever | ✅ 56.2 ms finito |
| Timers MFVDO/TOFF | ❌ desactivados | ✅ activos |
| Detección/clasificación PD | ❌ forzada (override) | ✅ real (SCCP) |
| Validación Vin en rango | ❌ | ✅ (50-58 V, clase 13) |
| Encendido | 4 a la vez, forzado/ciego | por puerto, cada uno **iff negoció** (independiente); los 4 pueden quedar activos si todos negociaron |

## Dependencia importante

Este bloque **no fuerza** potencia: entrega **solo si la clasificación SCCP
tiene éxito**. La SCCP fallaba (`PD_LINE_NOT_HIGH`) por el **problema de
hardware de la línea de baja** que Mayker está resolviendo. Orden correcto:

1. Mayker resuelve la línea de baja (Q18 / R103-R106 / polaridad) y ajusta la
   fuente a 50 V.
2. Aplicar estos dos cambios y compilar (sin las protecciones desactivadas).
3. Energizar **un puerto**, con el PD real, mirando corriente/temperatura.

Si se necesitara probar entrega **antes** de que SCCP funcione, usar
`ltc4296_force_port_pwr()` (fuerza salida pero **mantiene** TLIM, foldback,
soft-start; solo desactiva TMFVDO) — nunca volver al bloque con `TLIM_DISABLE`.
