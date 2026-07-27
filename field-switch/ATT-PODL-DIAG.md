# ATT alimentada por SPE (PoDL) — ✅ RESUELTO

> 2026-07-23. Síntoma: el field switch entregaba bien a un **PDM** pero con la
> **ATT** al otro extremo la negociación SCCP no completaba y la ATT no encendía.
> **RESUELTO: el condensador C5 de la ATT (~4.4 uF) asfixiaba la línea SCCP.**

## ✅ SOLUCIÓN: quitar C5 de la ATT

| Capacitancia entre los pines del conector SPE | Valor |
|---|---|
| **ATT original** | **4.46 uF** ← 8.6x el módulo que funciona |
| **PDM** (funciona) | 0.52 uF |
| **ATT tras quitar C5** | **0.05 uF** ✓ |

**Resultado tras quitar C5**, con el firmware de producción
`prebuilt/mfs_clean_class13.sbin` **sin ningún parche** (driver de ADI intacto):

```
P2ST = 0x3e12  -> DELIVERING + PI_POWERED + POWER_STABLE
Vin  = 49420 mV
Vout = 49385 mV   (caida de 35 mV)
```

**La ATT enciende alimentada por SPE, con negociación SCCP completa a 50 V.**

C5 puenteaba los dos conductores DC en el nodo de toma (antes de R14/R37),
cargando directamente la línea SCCP. El PDM funciona con 0.52 uF totales, así
que ese nivel basta para el filtrado de entrada; si se quiere conservar algo de
capacitancia por EMI, mantener el total **<= 0.5 uF**.

---

## Por qué fallaba (mecanismo, ya solo como referencia)


**Medido por SWD** (traza de la línea, 64 muestras cada 50 µs, grabada por el
propio firmware):

| Medida | Valor |
|---|---|
| Recuperación de la línea tras un bajo de 300 µs | **~850 µs** |
| Punto en que el driver de ADI muestrea cada bit de lectura | **700 µs** |
| Recuperación tras el pulso de reset (9 ms) | **~1100 µs** |

**La línea sube más lento de lo que el protocolo SCCP tolera**, y eso rompe la
comunicación en **ambos sentidos**:

- **PSE → PD (escritura):** un bit "1" se emite como bajo de 300 µs + alto. Con
  850 µs de subida, el PD muestrea cuando la línea **sigue baja** y lo lee como
  "0" → **el comando (`0xCC`/`0xAA`) llega corrupto** → el PD nunca responde con
  datos.
- **PD → PSE (lectura):** el driver muestrea a 700 µs, antes de que la línea se
  recupere → **todos los bits se leen como 0**.

Por eso el PD **sí contesta el pulso de presencia** (un bajo largo, inmune al
problema) pero **nunca envía la respuesta**: la respuesta leída es siempre
`0x000000` (muestreando a 700 µs) o `0xFFFFFF` (muestreando ≥1000 µs).

### Prueba de que el exceso de capacitancia está en la ATT

El **PDM negocia correctamente a través del MISMO PSM, el MISMO cable y el
MISMO firmware** (verificado a 24 V y a 50 V). Luego el lado PSE es
suficientemente rápido. **La diferencia está en la red de línea de la ATT.**

**Para Mayker:** comparar y **reducir la capacitancia** en la línea de la ATT
frente a la del PDM. Candidatos: **C88** (con R58), **C89**, **C90**, y el
acople de entrada (L2 + chokes FL2/FL3 + puente D13/D14). Nota: la capacitancia
de carga (entrada del buck) ya está aislada por el hot-swap Q6 durante la
clasificación, así que no es esa.

## Intentos de arreglo por FIRMWARE (agotados, documentados)

| Cambio probado | Resultado |
|---|---|
| Muestreo del pulso de presencia por **ventana** (en vez de un instante a 2 ms) | ✅ **Funciona** — la ATT pasa a ser detectada (`PD Not Present` → avanza). *Mejora genuina y correcta.* |
| Muestreo de bits a **1000 µs** (en vez de 700 µs) | ❌ Todos los bits salen `1` |
| Muestreo de bits a **1200 µs** | ❌ Todos los bits salen `1` |
| **T_W1L** acortado 300 µs → **120 µs** (subida más rápida en escritura) | ❌ El comando sigue corrupto |

⚠️ **NO llevar los cambios de timing a producción**: el PDM funciona con los
700 µs originales; muestrear más tarde **rompería el caso que sí funciona**. El
firmware de producción (`prebuilt/mfs_clean_class13.sbin`) mantiene el driver
original. La única mejora que valdría la pena portar (y validar contra el PDM)
es la **ventana de muestreo del pulso de presencia**.

## Hechos MEDIDOS — todo lo demás está SANO

| Comprobación | Resultado |
|---|---|
| Sondeo en el conector del PSM / conector SPE de la ATT / **C5** | **5 V** ✓ |
| **R14 / R37** | **0 Ω** ✓ |
| **Alimentación del LTC9111** (pin `IN`, medido en **C89**) | **4.85 V** ✓ |
| **IC2 pin 7 (`SCCP`)** con estímulo de pulsos repetidos | **CONMUTA** ✓ — el chip está vivo y responde |
| **R60 / R61** (3.3 Ω) | valor correcto ✓, y **pulso de 2.3 V** al conmutar → **Q1/Q4 conducen** ✓ |
| Pulso de presencia de la ATT (traza SWD) | **Presente**, a ~1.85 ms y de larga duración ✓ |
| Estado del puerto en clasificación | `PxST=0x2023` = SEARCHING ✓ |
| Vin (24 V / 50 V) | 23555 mV / 49455 mV ✓ dentro de ventana |

## Descartado con experimento (no volver a perseguirlo)

1. **Firmware de la ATT** — imposible: la detección/clasificación PoDL es
   hardware del LTC9111 y ocurre con la placa **sin alimentar**. Además el
   LTC9111 **no tiene interfaz digital** (ni SPI ni I²C): el STM32 no puede
   leerlo ni configurarlo, y por tanto **el USB/SWD de la ATT no sirve para
   diagnosticarlo**.
2. **Pin `EN` del LTC9111 sin conectar** — es una **salida**, no una entrada.
3. **Cable / PSM / camino DC** — 5 V llegan hasta el pin `IN` del chip.
4. **LTC9111 dañado** — el pin 7 conmuta: está vivo.
5. **Q1/Q4 y R60/R61** — pulso de 2.3 V sobre las resistencias: conducen.
6. **Compatibilidad de clases** — binpatch que acepta **cualquier** clase 10-15:
   sigue rechazando. Y la matriz cruzada con pruebas reales a PSE Clase 11 y
   Clase 13 descarta que la ATT reporte 10, 11 o 13.
7. **Clase / tipo SCCP / CRC** — irrelevantes: la respuesta nunca llega.

## Herramientas creadas en este diagnóstico

- **Consola serie del field switch**: COM8 @115200 por el mismo Pico
  (LPUART0B en el conector uC SWD). Con `CONFIG_LOG=y` el driver dice en texto
  qué falla (`PD Not Present`, `CRC ERROR`, `class not supported`, …). **Es la
  herramienta más útil que salió de esta sesión.**
- **Generador de pulsos de reset SCCP** por SWD (`pulse_gen.tcl`): estímulo
  periódico de 9 ms a ~17 Hz para medir con osciloscopio sin depender del
  firmware. ⚠️ Usar `adapter speed 500` — a 1000 el USB del Pico se cuelga.
- **Traza de la línea** grabada por el firmware en globales leídas por SWD
  (64 muestras × 50 µs) — así se midieron los 850 µs.

---

# Enlace de DATOS con la ATT — ABIERTO (2026-07-27)

Con la potencia ya resuelta, el siguiente escalón es el enlace 10BASE-T1L.
**Estado: no entrena.** Ambos extremos coinciden en cero portadora.

## Evidencia

| Extremo | Lectura |
|---|---|
| **ATT** (consola COM6) | `iface 1 dev=port_0 carrier=0 up=0` · `iface 2 dev=port_1 carrier=0 up=0` |
| **Field switch** (SWD) | `g_link[5] = 0` (los demás puertos = 1) |
| Conmutación del switch | **Funciona**: la TPU inyecta por macPort0 (RX 3920 B) y el switch replica a macPort1-4 (TX 3920 B c/u); **macPort5 queda excluido** por no tener enlace |

**Convención confirmada por los contadores:** en `g_link`, **1 = enlace ARRIBA**
y 0 = abajo (un puerto sin enlace queda fuera del flooding L2).

## Mapeo del slot (del esquemático MFS, J5)

**Slot Port 4 → datos `macPort5`** (nets `P5_RSTN/LINK/TXD/RXD`, y `PORT_SS_P5`)
y **potencia LTC port 2** (`P2_SW/SCCPI/SCCPO/PWR`). Los números de datos y de
potencia **NO coinciden** en el mismo slot: hay que mirar los nets, no la
serigrafía.

## Descartado con experimento

1. **Firmware ausente en la ATT** — falso: tenía firmware, y ahora corre el
   build actual (122 768 B, grabado y verificado byte a byte con
   `tools/stm32flash.py`). El **ADIN2111 se inicializa** y Zephyr crea sus dos
   interfaces de red.
2. **Conector SPE equivocado en la ATT** — probados ambos, sin cambio.
3. **Strap MDIO del PSM** — cambiado a **5** (el macPort del slot) y
   power-cycle del switch para que reconfigure: sin cambio.
4. **Relé de bypass K1** — el firmware lo gestiona (`PD14 = UC_BYPASS_EN` en
   alto al arrancar, con log de confirmación). No se pudo capturar ese mensaje
   de arranque (ver nota de captura abajo), así que **queda por verificar**.

## Siguiente candidato: MASTER/SLAVE del 10BASE-T1L

Un extremo debe ser **master** y el otro **slave**; si ambos piden el mismo rol
**el enlace nunca entrena**. Se descartó en su día con el PDM *porque entonces
el enlace SÍ entrenaba* — aquí no, así que vuelve a estar sobre la mesa.

Registros del ADIN1100 a leer en `macPort5`: `PHYID1` (1.0002, esperado
`0x0283`), `B10L link` (1.08F7), `AN status` (7.0201), `PMA_CTRL` (1.0834,
bit14 = master forzado), `AN_ADV_MST` (7.0203, bit4 = preferencia).

⚠️ El build que hace esa lectura **no arranca** (lotería de builds, 2 POR).
Reintentarlo o cambiar el enfoque (por ejemplo leer los registros vía SWD con
el SPI del ADIN6310 a pelo, como se hizo con el LTC4296).

## Notas de captura de consola de la ATT

- La ATT queda alimentada **también por el USB**: cortar los 50 V del SPE
  **no la reinicia**. Para ver su arranque hay que usar `Board RESET` con la
  captura ya abierta, o cortar ambas alimentaciones.
- Consola de la ATT: **COM6 @115200 8N1** (FT230X). El mismo puerto sirve para
  el **bootloader ROM** (115200, **paridad EVEN**) manteniendo `uC Bootload` y
  pulsando `Board RESET`.
- `tools/stm32flash.py` graba y **verifica releyendo la flash** — necesario
  porque el bootloader de esta placa falla ~1 de cada 2 escrituras.
