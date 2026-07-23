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

## Resolución de la línea de baja (actualizado 2026-07-22)

**Mayker indica que NO hay hardware que resolver en la línea de baja.** Es
decir, que el lado bajo (LGATE/Q18) "no se activara" **no es un fallo de HW**:
se resuelve por **firmware**, con el paso de **re-arme del disyuntor tras la
negociación** (ver sección siguiente) — que es justo lo que él pidió ("activar
el LGATE tras negociar"). Orden:

1. Ajustar la fuente a **50 V**.
2. Flashear `pse_safe_class13.sbin` (Clase 13 + eliminación del forzado +
   re-arme/verificación del lado bajo).
3. Con el PD real, verificar por SWD que `g_lg_any = 1` (negoció) y
   `g_lg_gfltev` bit0 = **0** (la baja queda enganchada y no dispara). Si aún
   disparara, se revisa entonces con los datos de `g_lg_pxev` (fwd/rev).

Este bloque **no fuerza** potencia: entrega solo si la clasificación SCCP tiene
éxito. Si se necesitara probar entrega **antes** de que SCCP negocie, usar
`ltc4296_force_port_pwr()` (mantiene TLIM/foldback/soft-start) — nunca volver al
bloque con `TLIM_DISABLE`.

## Paso lado bajo — re-arme/verificación del LGATE tras negociar (petición Mayker)

**Hallazgo del datasheet:** el LTC4296-1 tiene **un único `LGATE` compartido** (un
solo FET de retorno, Q18) — **no per-puerto** — y **no hay bit de firmware para
"encender el LGATE"**. El LGATE engancha automáticamente al entrar en power-up; el
disyuntor de baja compartido lo desconecta si dispara `LOW_CKT_BRK_FAULT`. Por
tanto "activar el LGATE tras negociar" se implementa así:

Bloque añadido en `main.c` (tras el monitoreo, antes de VIN/VOUT):
1. Detecta qué puertos **negociaron** (PxST bits 2:0 = 2 = DELIVERING).
2. Si **alguno** negoció, **re-arma el disyuntor de baja** (`ltc4296_clear_ckt_breaker`)
   para que Q18/LGATE enganche — **sin** tocar `MASK_LOWFAULT` (la baja queda
   visible y protegiendo). Si nadie negoció, **no** re-arma (correcto).
3. **Verifica**: lee `GFLTEV` (bit0 `LOW_CKT_BRK_FAULT`) y `PxEV` por puerto
   (bit0 `LSNS_REVERSE`, bit1 `LSNS_FORWARD`). Baja sin disparar = LGATE enganchado.

No fuerza potencia: solo actúa si hubo negociación. Si el HW de la línea de baja
sigue con fallo, el re-arme se vuelve a disparar y `g_lg_gfltev` bit0 lo delata.

### Reintento en el bucle principal (hot-plug de los 50 V) — añadido 2026-07-22
La negociación corre en `probe()` (arranque). Para no exigir un segundo POR al
conectar los 50 V en caliente, el bucle principal **reintenta cada ~5 s**:
por cada puerto que **aún no entrega** (PxST bits 2:0 ≠ 2) llama a
`ltc4296_retry_spoe_sccp()` (re-negocia por SCCP con la clase del devicetree);
los puertos que **ya entregan se saltan** (no se les molesta). Tras negociar,
re-arma/verifica el lado bajo. Así: **flashear sin 50 V → verificar arranque →
conectar 50 V + PD → en ≤5 s negocia solo** (sin segundo POR).

### Globals de verificación por SWD (build actual `build_final`, revalidar con el `.map`)
| Global | Dirección | Significado |
|---|---|---|
| `g_lg_deliver[4]` | `0x20097918` | 1 = puerto entregando (negoció) |
| `g_lg_any` | `0x20097914` | 1 = al menos un puerto negoció |
| `g_lg_rearm_rc` | `0x20097910` | retorno de `clear_ckt_breaker` (-1 = no negoció) |
| `g_lg_gfltev` | `0x2009917c` | GFLTEV tras re-arme (**bit0 = LOW_CKT_BRK_FAULT**) |
| `g_lg_pxev[4]` | `0x20099174` | PxEV por puerto (bit0 rev, bit1 fwd) |
| `g_lg_st[4]` | `0x2009916c` | PxST por puerto |
| `g_retry_rc[4]` | `0x20097900` | rc de `retry_spoe_sccp` en el bucle (`ADI_LTC_SCCP_COMPLETE`=éxito) |

**Criterio de éxito:** `g_lg_any=1` (negoció), `g_lg_gfltev` bit0 = **0** (la baja no
dispara tras re-armar) y `g_lg_pxev[q]` bits 0/1 = **0** en el puerto activo.

### Pendiente de aclarar con Mayker
El LGATE es **compartido**, no per-puerto, y **no hay control de firmware directo**.
Confirmar si con "activar el LGATE de ese puerto" se refería a esta secuencia
(power-up tras negociar + re-arme del retorno, ya implementada) o a un mecanismo
específico (GPIO / pin `AUTO` / mod de hardware) que haga el retorno per-puerto.

## Artefacto
`prebuilt/pse_safe_class13.sbin` — firmado (rom_version 010203ff, jump 0x100064b8),
Clase 13 + bloque forzado eliminado + paso de re-arme/verificación del lado bajo.
**Compilado, NO flasheado.**

## Mejoras post-auditoría (2026-07-23) — aplicadas y compiladas

1. **Eliminado el bloque residual "(1) Línea SCCP"** (hacía prebias +
   classification en el puerto 3 sin validar Vin — la brecha divulgada en
   `LTC4296-BURN-AUDIT.md`). Ya no hay NINGUNA escritura al LTC4296 fuera del
   camino del driver con compuertas.
2. **Aserción de protecciones al arrancar**: lee `GCFG` y, solo si la lectura
   es válida (≠0xffff), limpia `TLIM_DISABLE` (bit4) y `MASK_LOWFAULT` (bit5).
   Garantiza límite térmico activo y línea de baja visible en cada boot, sea
   cual sea el estado previo del chip. Estado final en `g_gcfg` (SWD).

Artefacto: `prebuilt/pse_safe_class13.sbin`, jump `0x100064f8`. **Compilado y
firmado, pendiente de flashear cuando se conecte el power switch** (y de
estrenar con el protocolo de energización + experimento del MCU en reset).
Direcciones SWD de este build: `g_gcfg=0x20099178`, `g_lg_any=0x20097914`,
`g_lg_gfltev=0x20099170`, `g_retry_rc=0x20097900`.
