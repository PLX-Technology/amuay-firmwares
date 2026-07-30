# ATT sin enlace de datos: **el relé K1 no engancha**

**Para Mayker.** Diagnóstico del 2026-07-30.

**Resumen:** la ATT recibe potencia por SPE y su firmware corre, pero **no hay
enlace de datos**. El relé **K1 no se activa** (no hace clic), así que el
ADIN2111 se queda **fuera de la línea** y el par pasa de largo. El firmware
está correcto por los dos extremos: **es un fallo de hardware entre `PD14` y
la bobina de K1**.

---

## 1. Por qué K1 lo explica todo

Del propio código (`att/src/main.c`):

> `BYPASS: PD14 (UC_BYPASS_EN) en ALTO energiza el relé K1 y mete el ADIN2111
> en la línea SPE. Sin esto NO hay link jamás (el relé arranca puenteando
> P1<->P2).`

En reposo K1 **puentea los dos conectores SPE**: la señal entra por uno y sale
por el otro **sin tocar el PHY**. Por eso la falta de portadora es **mutua y
simétrica**, que es justo lo medido.

## 2. Lo medido en los dos extremos

Registros del PHY leídos con el mismo criterio a ambos lados:

| | MFS (puerto de la ATT) | ATT (sus dos PHY) |
|---|---|---|
| PHYID | `0283:bc81` | `0283:bca1` |
| Autonegociación | `ANctl = 0x1000` → **ON** | `ANctl = 0x1000` → **ON** |
| AN status | `0x0008` (solo *AN ability*) | `0x0008` (solo *AN ability*) |
| PMA control | `0x8002` | `0x8002` |

**Los dos extremos están bien configurados, los dos emiten, y ninguno oye al
otro.** Y la **potencia sí pasa** por ese mismo par (el SPoE negocia y
entrega): hay continuidad DC, pero el camino de datos está interrumpido.

## 3. Qué está descartado, con medidas

- **`phyPullupCtrl`** — el MFS identifica su ADIN1100 (`0x0283`/`0xbc81`) y
  tiene la autonegociación encendida.
- **Orden de arranque del relé** — era un defecto real y **ya está
  corregido**: K1 se cierra ahora en un `SYS_INIT` a `POST_KERNEL/55`, antes
  del driver del PHY (`CONFIG_PHY_INIT_PRIORITY = 60`, verificado en el
  `.config`). No era la causa.
- **Presupuesto de potencia** — falla igual con la ATT alimentada desde banco.
- **El cambio de MAC** — la secuencia baja las interfaces y **las vuelve a
  subir**; el `up=0` del log es consecuencia del `carrier=0`, no un fallo
  aparte.
- **Conflicto de pin** — `PD14` **no aparece en el devicetree**: ni nodo ni
  `pinctrl`. El pin está libre y lo gobierna el driver GPIO.
- **La escritura al GPIO** — el log dice
  `UC_BYPASS_EN (PD14) = 1 -> rele K1 energizado (ret=0)`.

Es decir: **el firmware pide cerrar el relé, la llamada tiene éxito, y el relé
no cierra.**

## 4. Qué medir, por orden

1. **`PD14` en el pin del micro**, con la placa arrancada.
   - **~3.3 V** → la GPIO cumple; el fallo está aguas abajo (pasos 2 y 3).
   - **~0 V** → el pin no está mandando pese al `ret=0`; mirar carga excesiva
     o corto en esa red.
2. **La etapa de mando del relé.** Una GPIO **no puede** excitar una bobina
   directamente: tiene que haber transistor/MOSFET y su diodo de recirculación.
   Comprobar que el transistor conduce con `PD14` alto.
3. **El raíl que alimenta la bobina.** Si K1 necesita una tensión (5 V, 12 V)
   que no está presente, no engancha aunque el mando sea correcto.
4. **Continuidad**: con K1 supuestamente cerrado, que los pines del par del
   ADIN2111 queden conectados al conector SPE.

## 5. Nota

**Esto no es una regresión de firmware.** Los dos lados están correctamente
configurados, así que los cambios de los últimos días probablemente
**destaparon** el fallo (al pasar la ATT a alimentarse por SPE, que fuerza
arranque en frío siempre) en vez de causarlo.

## 6. Instrumentación disponible

- `att/src/main.c` — vuelca por consola (**115200**, no 9600) los registros
  del PHY de los dos puertos cada segundo: `att_phy_dump()`.
- `tools/ltc4296-swd/mfs_phy6.tcl` (rama `mps`) — lo mismo por SWD en los seis
  puertos del MFS.

⚠️ **Indicadores que NO sirven**, para no perder tiempo: `g_link` del MFS da
**links fantasma** en slots vacíos (leen `1` sin nada conectado; el PHY va
dentro del módulo, así que sin módulo el MDIO flota alto). Y el LED del PSM es
indicador de **potencia**, no de enlace.
