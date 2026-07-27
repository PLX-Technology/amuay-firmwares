# ATT alimentada por SPE (PoDL) — diagnóstico en curso

> 2026-07-23. Síntoma: el field switch entrega bien a un **PDM** (verificado a
> 24 V Clase 11 y a 50 V Clase 13), pero con la **ATT** al otro extremo del
> mismo PSM y cable, **la negociación no completa y la ATT no enciende**.

## Front-end PD de la ATT (esquemático `Downloads/ATT.png`)

```
conector SPE → FL2/FL3 (chokes) → R14/R37 (0 Ω) → L2 (PID100-650M)
   → puente D13/D14 (PMEG10020AELRX) → nodo INLTC9111 (C89, C90, R59, TVS D15)
   → IC2 LTC9111RDE#PBF (pin 3 IN)
respuesta SCCP: IC2 pin 7 SCCP → Q1/Q4 (BSS123) + R60/R61 (3.3 Ω) sobre SNS1/SNS2
alimentación: OUT/GATE → Q5 (PSMN075-100MSEX) → D8 → POWER_SPE → buck PS1
              (TPS54561DPRR, entrada 4.5-60 V; riel etiquetado 7-60 VDC)
```

Straps de clase: **`CLASSC` (pin 10) fijo a GND**; **`CLASSV` (pin 5) → JP2**,
seleccionable a `STBY` o `GND`. Serigrafía del bloque: "CLASS 10".
- `CLASSV`=GND → Clase 10 · `CLASSV`=STBY → **Clase 13**
- Mayker confirma pines 4-5 unidos = `CLASSV`=`STBY` → debería ser **Clase 13**.

## Hechos MEDIDOS (no hipótesis)

| Comprobación | Resultado |
|---|---|
| Sondeo de detección en el conector del PSM | **5 V** ✓ |
| Sondeo en el conector SPE de la ATT | **5 V** ✓ (cable sano) |
| Sondeo en **C5** de la ATT | **5 V** ✓ |
| **R14 / R37** | **0 Ω** ✓ (camino de baja impedancia) |
| Línea SCCP en reposo / pull-down del PSE | **alta / baja** ✓ (protocolo OK) |
| Estado del puerto en clasificación | `PxST=0x2023` = **SEARCHING** ✓ |
| Vout del puerto durante sondeo | **5110 mV** (= sin carga; un PD con firma válida bajaría a 4.05-4.55 V) |
| **Osciloscopio en la ATT** (Mayker) | **transacción SCCP completa visible**, luego la tensión cae a 0 |
| Vin a 24 V (Clase 11) | **23555 mV** ✓ dentro de ventana → la negociación SÍ corre |

## Descartado con experimento (no volver a perseguir)

1. **Firmware de la ATT** — imposible: la detección/clasificación PoDL es
   hardware del LTC9111 y ocurre con la placa **sin alimentar** (el STM32 está
   muerto). Ningún firmware la habilita o deshabilita.
2. **Pin `EN` del LTC9111 sin conectar** — es una **SALIDA** digital (habilita
   el DC/DC del PD), no una entrada. Dejarlo al aire es correcto.
3. **Cable / PSM / camino DC de entrada** — 5 V llegan hasta C5 y R14/R37=0 Ω.
4. **R14/R37 de valor alto** (atenuando la señal SCCP) — medidas en 0 Ω.
5. **Compatibilidad de clases** — probado con un binpatch de datos que hace
   `class_compatibility` aceptar **cualquier** clase 10-15: **sigue
   rechazando**. Además, la matriz cruzada con dos pruebas reales descarta que
   la ATT reporte 10, 11 o 13:

   | Si la ATT reportara | PSE Clase 13 | PSE Clase 11 | ¿Compatible con lo observado? |
   |---|---|---|---|
   | 10 | rechaza | **aceptaría** | ❌ |
   | 11 | rechaza | **aceptaría** | ❌ |
   | 13 | **aceptaría** | rechaza | ❌ |
   | 12 / 14 / 15 | rechaza | rechaza | ✓ |
   | CRC o tipo SCCP inválido | rechaza | rechaza | ✓ |

## Causa restante (a confirmar): la RESPUESTA SCCP no es la esperada

El driver (`ltc4296_sccp.c: sccp_is_pd`) valida la respuesta en **tres
compuertas**, y solo se ha descartado la tercera:

1. **Tipo**: `(respuesta >> 12)` debe ser exactamente **`0xC`**
   (`sccp_types[4]`) — si no, `PD_CLASS_NOT_SUPPORTED`.
2. **Código de clase**: los 12 bits bajos deben coincidir con una entrada de
   `sccp_classes[]` (clase 13 = `0x004`); si no coincide, `pd_type` queda en 0
   → rechazado.
3. **Compatibilidad** `class_compatibility[pd_type][pse_class+10]` — ✅ descartada.

Y antes de todo eso, `sccp_read_write_pd` puede devolver **`CRC_FAILED`**.

**Siguiente paso: leer los 3 bytes crudos de la respuesta** (2 de datos + CRC).
Dos vías:
- **(a) Firmware instrumentado**: capturar `sccp_buf[0..2]` y el código de
  retorno en globales leídas por SWD. ⚠️ Bloqueado: los builds instrumentados
  **no arrancan** (ver más abajo).
- **(b) Osciloscopio**: decodificar los bits de la respuesta. Timings del
  bit-bang (`ltc4296_sccp.c`): reset 9 ms; **write-1** = low 300 µs + high
  2150 µs; **write-0** = low 2450 µs; **read slot** = low 300 µs, release,
  muestreo a 1 ms, total ~3.3 ms. Transacción completa ≈ 140-150 ms (encaja
  con la captura de Mayker a 20 ms/div). Capturar a ~2 ms/div la parte final
  (los 3 bytes de respuesta) y medir anchos de pulso.

## ⚠️ Bloqueo lateral: los builds instrumentados no arrancan

Los builds que **modifican** la imagen conocida-buena (retry de SCCP en el
bucle; instrumentación del driver) **se quedan en ROM tras el POR**, mientras
que el build limpio arranca de forma fiable:

| Build | Arranca |
|---|---|
| `mfs_clean_class11` | ✅ (varias veces) |
| `mfs_clean_class13` (no-retry) | ✅ |
| class13 + retry SCCP en el bucle | ❌ (5 POR: 3 a 50 V, 2 a 24 V) |
| class13 + instrumentación del driver | ❌ (2 POR) |

Descartado: firma (verificada criptográficamente válida), flash (verify OK),
`imglen`/jump/vectores (correctos), placa (el build limpio arranca siempre).
**Causa aún desconocida.** Los **binpatches de datos** sobre la imagen buena SÍ
arrancan (así se hicieron los cambios de clase y el test de `class_compatibility`),
así que ese es el único camino fiable para modificar hoy.

## Estado del banco al cerrar

- Field switch con `prebuilt/mfs_clean_class11.sbin` (limpio, sin parches).
- Fuente a 24 V. ATT cableada al PSM del slot Port 4.
- Para volver a 50 V: `prebuilt/mfs_clean_class13.sbin` (piso Vin 49 V) y
  **arrancar con los 50 V ya presentes** (la negociación es al boot).
