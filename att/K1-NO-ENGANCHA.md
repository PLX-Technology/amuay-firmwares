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

### ✅ `PD14` YA ESTÁ COMPROBADO: entrega 3.3 V en el pin

**No hace falta medirlo.** El firmware **relee el nivel físico del pin** (en el
STM32 el registro de entrada refleja el pin aunque esté configurado como
salida) y saca por consola:

```
PD14 releido del pin = 1   (la GPIO cumple: mirar aguas abajo)
PD14 = 1                   ← estable, cada segundo
```

⇒ **La GPIO cumple.** El fallo está **entre `PD14` y los contactos del relé**.

### Lo que queda por medir

1. **La etapa de mando del relé.** Una GPIO **no puede** excitar una bobina
   directamente: tiene que haber transistor/MOSFET y su diodo de
   recirculación. Comprobar que el transistor conduce con `PD14` alto.
2. **El raíl que alimenta la bobina.** Si K1 necesita una tensión (5 V, 12 V)
   que no está presente, no engancha aunque el mando sea correcto.
   ⚠️ **Ya descartado que dependa del SPoE**: no hace clic ni alimentando la
   ATT **solo por SPE** ni por banco.
3. **El propio relé** (bobina abierta, contactos).
4. **Continuidad**: con K1 supuestamente cerrado, que los pines del par del
   ADIN2111 queden conectados al conector SPE.

## 5. ★ El enlace FUNCIONABA el 28, así que K1 enganchaba entonces

La pasarela de la TPU registra datos del `tank21` por SPE el **28/07/2026 a
las 17:31:43**. O sea que **el relé K1 sí se activaba ese día**: no es un
defecto de diseño ni un relé muerto de fábrica, es **algo que se rompió entre
el 28 y el 30**.

Y eso exonera a los tres cambios de firmware de ese día, porque los datos
llegaron **después** de todos ellos:

| | |
|---|---|
| 28/07 11:45 | MCUboot + OTA + trama v3 |
| 28/07 12:46 | MAC derivada del UID |
| 28/07 15:00 | vigilante de enlace SPE |
| **28/07 17:31** | **la pasarela recibe datos** ✅ |
| 29/07 08:44 | se retira el vigilante |

Además, **solo cuatro commits han tocado `BYPASS_EN_PIN`** en toda la
historia, y el par añadir/retirar el vigilante tiene **diff neto vacío**
(`git diff 73391e7^ 21ab9b0 -- att/src/main.c` no devuelve nada). La
escritura a `PD14` en `main()` **no se ha tocado desde el 15 de julio**.

⇒ El firmware que gobierna K1 es **el mismo que funcionaba el 28**. Con la
ATT alimentada desde banco, hoy el relé no engancha. **Buscar un fallo
físico aparecido en esos dos días**: relé, transistor de mando, o su raíl.

### Ojo con la identidad: la MAC cambió

La trama del 28 llegó con MAC **`02:00:00:ad:21:11`** (asignada a mano — la
`spe0` de la TPU es `02:00:00:ad:11:10`, misma familia). La ATT de hoy deriva
**`02:00:70:2d:30:08`** del UID del silicio. El registro del tanque en la
pasarela está indexado a la identidad vieja, así que **cuando el enlace
vuelva, revisar que la pasarela reconozca la MAC nueva** o seguirá marcando
"sin señal" aunque lleguen tramas.

Detalle de cronología: a las 17:31 del 28 la trama aún llevaba la MAC vieja,
pese a que el commit de la MAC-por-UID es de las 12:46 — la ATT todavía no
tenía grabado ese firmware a esa hora.

## 6. Instrumentación disponible

- `att/src/main.c` — vuelca por consola (**115200**, no 9600) los registros
  del PHY de los dos puertos cada segundo: `att_phy_dump()`.
- `tools/ltc4296-swd/mfs_phy6.tcl` (rama `mps`) — lo mismo por SWD en los seis
  puertos del MFS.

⚠️ **Indicadores que NO sirven**, para no perder tiempo: `g_link` del MFS da
**links fantasma** en slots vacíos (leen `1` sin nada conectado; el PHY va
dentro del módulo, así que sin módulo el MDIO flota alto). Y el LED del PSM es
indicador de **potencia**, no de enlace.
