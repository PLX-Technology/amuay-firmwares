# MPS — Power switch

Ficha técnica y procedimiento de instalación.
Rev. 2026-08-11 · Sistema de monitoreo de tanques Varec

> ⚠️ **[Mayker]** = pendiente de fabricación (mecánica, conectores, LEDs,
> rangos ambientales). El resto está medido o verificado en banco.

---

## 1. Qué hace

Cabecera de la red SPE. Toma la alimentación de planta y **reparte 50 V y
datos** a los field switches. Es el único equipo que se alimenta de la red
eléctrica; todo lo que hay por debajo vive de él.

| | |
|---|---|
| Conmutador | **ADIN6310** |
| Microcontrolador | **MAX32690** (Cortex-M4), firmware Zephyr |
| Controlador PSE | **LTC4296** (SPoE clase 13) |
| Slots SPE | **4** |
| Salida por puerto | 50 a 58 V, clase 13 |
| Enlace a la pasarela | Ethernet a la TPU |

---

## 2. Consumo — medido 2026-08-11

Lo relevante del MPS es lo que **entrega**, no lo que gasta:

| Medida | Valor |
|---|---|
| Un field switch con 4 sensores y otro switch encadenado | **115,2 mA / 6,20 W** |

**Para dimensionar la fuente:** sumar el consumo de cada rama colgada de cada
slot. Un field switch con 5 sensores ronda 95 mA / 5 W (ver ficha del MFS).

**[Mayker]** consumo propio de la placa, tensión y corriente de entrada, y
capacidad máxima por puerto y total.

---

## 3. Instalación

### 3.1 Antes de ir a campo

1. **Grabar el firmware por cable, en el banco.**
2. **Comprobar que el ADIN6310 está aprovisionado.** Una placa nueva lo trae en
   blanco: arranca pero ningún puerto funciona.
   ⚠️ Ese paso se hace **con los 50 V desconectados**: la imagen de
   aprovisionamiento es anterior a las protecciones y **energiza sin
   negociar** — es la que quemó dos LTC4296.
3. **Straps MDIO y DIP `RXD0`** de cada módulo, según el tramo al que vaya.

### 3.2 En campo

1. Montar los módulos **PSM** en los slots que se usen.
2. Conectar la Ethernet a la TPU.
3. Conectar los cables de cada field switch a sus slots.
4. Dar alimentación. Cada puerto detecta, clasifica y **entonces** entrega.

⚠️ **Comprobar la polaridad del par en el zócalo antes de energizar.** Un par
invertido impide negociar y el puerto no entrega nunca. Ya ocurrió en este
equipo. **[Mayker]** confirmar el marcado del zócalo.

### 3.3 Verificación

| Comprobación | Dónde se ve |
|---|---|
| El MPS aparece vivo | Pasarela, sección de equipos |
| Cada slot ocupado pasa a **entregando**, con su corriente | Pasarela, consumo por slot |
| Cada field switch aparece por debajo | Pasarela, árbol de topología |

Un slot ocupado que se queda en **deshabilitado** o **buscando** y no llega a
entregar señala un problema en el módulo, el cable o el equipo del otro
extremo — no en el MPS.

---

## 4. Fallos conocidos

| Síntoma | Causa probable |
|---|---|
| Arranca pero **ningún puerto funciona** | ADIN6310 sin aprovisionar |
| Un puerto no negocia nunca | **Par invertido** en el zócalo, o módulo en corto |
| Un puerto entrega y el equipo no aparece en la pasarela | Strap MDIO del módulo equivocado |
| Un puerto queda **inactivo** sin reintentar | Estado muerto del LTC4296; el firmware actual ya lo evita |
| Avisa de `Vin fuera de rango` | Está por debajo de 50 V: la clase 13 los exige |

---

## 5. Límites y avisos

- **El firmware NUNCA entrega potencia sin negociar**, y no debe cambiarse:
  sin negociación se han quemado dos LTC4296.
- La lectura de tensión de entrada tiene un **error de ganancia conocido**
  (≈ −2 % en este equipo). Afecta a la potencia estimada, no a la seguridad.
- **La potencia mostrada es estimada**: corriente medida por la tensión de
  entrada, y esa tensión solo se refresca en los puertos que **no** entregan.
  Con todos los slots cargados, el valor se congela en la última medida.

**[Mayker]** dimensiones, fijación, grado de protección, rango de temperatura,
LEDs, referencia de conectores, entrada de alimentación, certificación de zona.
