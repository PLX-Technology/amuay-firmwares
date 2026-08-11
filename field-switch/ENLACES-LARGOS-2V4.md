# Tramos SPE largos: el nivel de 2,4 Vpp y el cable

2026-08-11. Investigación completa sobre por qué los enlaces de 900-1000 m de
la refinería no suben. **Los equipos estaban bien; el cable no.**

---

## 1. El resultado, en corto

| | |
|---|---|
| **Causa raíz** | El cable. Instrumentación de 4 hilos sueltos, sin par trenzado ni impedancia especificada |
| **Los switches** | Correctos: negocian 2,4 Vpp y enlazan con un latiguillo decente |
| **Lo que hay que hacer en firmware** | **Nada** |
| **Lo que hay que hacer en hardware** | Abrir el interruptor de `RXD0` del DIP, **en los dos extremos** |
| **Lo que hay que comprar** | Par trenzado 18 AWG, 100 Ω, ≤45 nF/km (fieldbus Type A) |

---

## 2. El nivel de transmisión: se negocia, no se elige

10BASE-T1L tiene dos amplitudes: **1,0 Vpp llega a ~200 m** y **2,4 Vpp a
1000 m**. El nivel lo fija la autonegociación al enlazar, y sube a 2,4 V solo
si **los dos extremos son capaces** y **al menos uno lo pide**.

### 2.1 ⚠️ El registro es el `0x0204`, no el `0x0203`

El anuncio BASE-T1 son 48 bits en **tres** registros del MMD 7:

| registro | | |
|---|---|---|
| `0x0202` | ADV_L `[15:0]` | |
| `0x0203` | ADV_M `[31:16]` | bit 14 = "soy compatible con 10BASE-T1L" |
| **`0x0204`** | **ADV_H `[47:32]`** | **bit 13 = capaz de 2,4 V · bit 12 = lo pide** |

Confundirlos costó una jornada entera. Leíamos `0x0203 = 0x4000` y lo
tomábamos por "capaz pero sin pedirlo", cuando ese bit solo dice que el PHY
habla 10BASE-T1L y está puesto **siempre**. Las escrituras del supuesto bit de
petición caían en un bit reservado: no cambiaban nada, y de ahí salió la teoría
falsa de que el PHY las revertía desde sus straps.

Referencia que lo zanja: `MDIO_AN_T1_ADV_H_10L_TX_HI_REQ` en
`include/uapi/linux/mdio.h` (`MDIO_AN_T1_ADV_H` = 516 = `0x204`).

### 2.2 ★ Se habilita por HARDWARE: el DIP de `RXD0`

En el ADIN1100 de cada módulo hay un DIP de 4 posiciones (CVS-04B, pull-ups de
4,7 kΩ a VDD3P3) con `PHYAD_2/1/0` y **`RXD0`**. **Abrir `RXD0`** habilita la
capacidad real del PHY, y de ahí sale todo en cascada:

```
DIP abierto -> 0x08F7 bit12 = 1 (capaz)  ->  advH = 0x3000 (capaz + pide)
            -> negocia 2,4 V  ->  0x08F6 bit12 = 1
```

Medido en el field switch `881e`: con el DIP cerrado, `pmastat = 0x2800`
(sin el bit 12) y `advH = 0x0000`. Con el DIP abierto, `pmastat = 0x3800` y
`advH = 0x3000`.

**Escribir el anuncio desde el firmware es un no-op**: donde el strap está
abierto ya viene puesto, y donde está cerrado el PHY ni siquiera es capaz.

⚠️ **En los DOS extremos** de cada tirada larga, **incluidos los módulos PDM**
entre switches, no solo los puertos de sensor.

---

## 3. Cómo se mide (procedimiento para validar un cable)

Firmware con lectura periódica de `0x0108F6` **con el enlace ya arriba**.

⚠️ **La lectura del arranque NO vale**: antes de enlazar, `0x08F6` solo refleja
el valor por defecto del strap. El nivel de verdad lo fija la negociación.

| lectura | veredicto |
|---|---|
| `link=1` · `0x08F6` bit12 = 1 · `anst=0x002c` | negociaron 2,4 V. **Cable aprobado** |
| `link=1` · bit12 = 0 | el extremo **remoto** no concede 2,4 V |
| `link=0` · `anst=0x0008` | el PHY **no ve nada** al otro lado, igual que un puerto vacío. **Cable rechazado** |

Bits de `anst` (MMD 7 `0x0201`): 2 = link, 3 = capaz de AN, 5 = AN completa.

**Vale la pena hacerlo con un carrete de muestra antes de comprar kilómetros.**
Diez minutos.

---

## 4. El cable que NO sirve

Carrete **Teldor `8296C04101D100T` — `INS 4x18# ETFE`**, 1000 m. Cuatro
conductores sueltos de 18 AWG, cable de instrumentación.

| medida | valor | comentario |
|---|---|---|
| Resistencia de bucle | **40 Ω** | correcto para 18 AWG a 1000 m |
| Capacitancia entre hilos | **70 nF** = 70 pF/m | **más del doble** de los ~30 nF/km del Type A |
| Par trenzado | **no** | cuatro colores sueltos, sin pares |

Resultado con los dos extremos a 2,4 Vpp confirmados: **`anst=0008`, el PHY no
ve nada.** No es un enlace marginal, es que no llega señal.

**Y la continua sí pasa**: el switch entrega 30 mA / 1,6 W por ese mismo cable.
Continuidad hay; ancho de banda no. Ese contraste es el que despista, y es la
firma de "el medio no vale", no de "hay un corte".

Los **80 m** probados antes no discriminan: a esa distancia funciona casi
cualquier cosa, incluso a 1,0 Vpp.

### 4.1 Por qué paralelar hilos no lo arregla

Usar dos conductores por lado baja la resistencia a la mitad, pero **duplica la
capacitancia** y hunde más la impedancia característica (`Z0 = sqrt(L/C)`:
menos L, más C). El cuello de botella no es la resistencia —la potencia ya
cruza— sino la capacitancia. Empeora.

Y sin trenzado no hay rechazo de modo común, que es lo que hace funcionar la
transmisión diferencial a distancia. Eso no lo arregla ninguna combinación.

---

## 5. Especificación de compra

- **Par trenzado individual**, 18 AWG
- **Impedancia característica 100 Ω**
- **Capacitancia mutua ≤ 45 nF/km** (el Type A anda por 30)
- Apantallado, con la certificación que exija la clasificación de zona
- Vendido como **10BASE-T1L / SPE** o **fieldbus Type A (IEC 61158-2)**

---

## 6. Pendiente

El módulo del **`macPort4`** del `881e` (= slot **Port 5** de serigrafía) **no
declara capacidad de 2,4 V ni con el DIP abierto**: `pmastat = 0x2800` frente
al `0x3800` de sus cinco hermanos. Defecto de ese módulo — interruptor sin
contacto, resistencia de 4,7 kΩ mal soldada, o el propio PHY. Para Mayker.

---

## 7. Descartado por el camino (para no repetirlo)

- **No es el firmware.** La inicialización de puertos no toca el nivel, y no
  tiene por qué.
- **No es el switch remoto.** Con latiguillo corto negocia y concede 2,4 V.
- **No es un terminador de fieldbus** en la línea: serían ~1 µF, medimos 70 nF.
- **No es un hilo partido ni un empalme malo**: 40 Ω de bucle, impecables.
- **No es que la negociación no cruce por ir a 1,0 V.** La AN va en modo de
  baja velocidad (DME a 625 kb/s), mucho más robusta que los datos. Era una
  hipótesis razonable y resultó falsa.
- **No es la bobina.** Un par trenzado bobinado enlaza; el arrollamiento afecta
  al modo común, no al diferencial.
