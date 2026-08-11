# ATT — Sensor de nivel sobre Varec 2500

Ficha técnica y procedimiento de instalación.
Rev. 2026-08-11 · Sistema de monitoreo de tanques Varec

> ⚠️ **[Mayker]** = pendiente de fabricación (mecánica, conectores, LEDs,
> rangos ambientales). El resto está medido o verificado en banco.

---

## 1. Qué hace

Va **dentro del cabezal del Varec 2500**. Lee el encoder acoplado al eje del
cuadrante y envía la cuenta por SPE cada 500 ms. **No calcula el nivel**: manda
la cuenta cruda y la conversión a milímetros la hace la pasarela.

| | |
|---|---|
| Microcontrolador | **STM32WBA65** |
| Interfaz SPE | **ADIN2111** (2 puertos 10BASE-T1L) |
| Temperatura | **ADT75** (I²C) |
| Identidad | **EEPROM I²C AT24C64D** — guarda el `tank_id` |
| Arranque | **MCUboot** — permite actualización remota |
| Alimentación | por SPE desde su field switch |

**El `tank_id` vive en la EEPROM del sensor**, no en la pasarela. Si se
sustituye una placa averiada, se le pone el número del tanque y listo.

⚠️ **La calibración NO vive aquí**: está en la pasarela, por tanque. Cambiar la
placa no pierde la calibración; borrar la placa tampoco la recupera.

---

## 2. Encoder

| | |
|---|---|
| Señales | A y B en cuadratura |
| Resolución | **512 cuentas por vuelta** (128 PPR × 4) |
| Cabezal **inglés** | 1 vuelta = 1 ft = 304,8 mm → **0,595312 mm/pulso** |
| Cabezal **métrico** | 1 vuelta = 100 mm → **0,195313 mm/pulso** |

⚠️ Esos valores suponen que el encoder va **al eje del cuadrante**. El acople
es nuestro, no de Varec: son un **punto de partida**, no una verdad. Se
confirma en campo moviendo la cinta con la perilla de comprobación y
dividiendo el desplazamiento del contador mecánico entre los pulsos contados.

---

## 3. Consumo — medido 2026-08-11

| Unidad | Corriente | Potencia |
|---|---|---|
| Rango observado (4 sensores) | **9,6 – 27,6 mA** | 0,50 – 1,46 W |
| Valor típico para dimensionar | **~12 mA** | ~0,65 W |

La dispersión es real y conviene conocerla: el consumo sube si el **relé K1**
queda energizado. Si un sensor consume el doble que sus hermanos, es lo primero
que hay que mirar.

---

## 4. Instalación

### 4.1 Antes de ir a campo

1. **Grabar el firmware por cable** — es la **única visita obligatoria** de la
   placa. Después se actualiza por SPE desde la pasarela.
   ⚠️ El grabado inicial debe llevar ya el **MCUboot correcto**: el arranque
   no se puede actualizar en remoto.
2. **Asignar el `tank_id`**, en el banco o desde el panel una vez conectada.

### 4.2 En el cabezal

1. Montar la placa y acoplar el encoder al **eje del cuadrante**.
2. Conectar el par SPE al PSM de su field switch.
3. El sensor arranca cuando el switch negocia y entrega.

### 4.3 ⚠️ Reglas de seguridad eléctrica

- **NUNCA unir la masa de la ATT con la masa del field switch.**
- **NUNCA forzar a nivel bajo la línea TX del RS-485.**
- Si la instalación va a un tramo largo, el módulo del switch necesita el
  **DIP `RXD0` abierto** en los dos extremos (ver ficha de módulos).

### 4.4 Puesta en marcha

1. Comprobar que el sensor **aparece en línea** en la pasarela.
2. **Calibrar**, desde el panel o desde la plataforma web.

**Calibración por geometría** — la recomendada, porque **no hay que mover
producto**, que es lo que hace viable dar de alta 50 tanques:

```
nivel = altura de referencia − pulsos × mm por pulso
```

Se necesitan dos datos: los **mm por pulso** (tabla del punto 2, editables) y
la **altura de referencia**, que es la cota del cero del contador y se mide con
cinta métrica.

La de **dos puntos** es más exacta pero exige mover el flotador entre dos
niveles conocidos. Queda para cuando el tanque se llene o se vacíe por
operación y se pueda aprovechar el movimiento.

⚠️ **Un tanque sin calibrar publica CUENTAS del encoder, no milímetros.** La
pasarela lo marca con `calibrado: false` y pone a nulo todos los valores de
calibración. No es un nivel: es una cuenta.

---

## 5. Fallos conocidos

| Síntoma | Causa probable |
|---|---|
| No aparece en la pasarela | Sin enlace SPE, o el switch no le entrega |
| Aparece pero el nivel no cambia | Encoder no acoplado, o el cabezal no se mueve |
| Nivel absurdo pero creíble | **Sin calibrar**, o unidades equivocadas en la altura de referencia |
| Consume el doble que sus hermanos | Relé **K1** energizado |
| Se queda mudo sin motivo | Revisar `F_BAT`: en algunas placas la detección de batería está invertida y la deja callada al arrancar |

⚠️ **`F_BAT` no se activa a ciegas.** El pin de detección no significa lo mismo
en todas las placas: se ha medido una que da lo contrario que otra en la misma
situación. Activarlo en una placa invertida la deja **muda nada más arrancar**,
e indistinguible de un sensor averiado. Comprobar antes en **esa** placa.

---

## 6. Actualización remota

Por SPE desde la pasarela: pestaña **Firmware** del modal de cada tanque.

La imagen nueva arranca **a prueba** y solo se confirma tras 4 envíos SPE
correctos. Si arrancara sin transmitir, el arranque revierte sola a la
anterior: **una actualización no puede dejar un sensor mudo dentro de un
tanque**.

⚠️ **MCUboot no se actualiza por esa vía** — vive en su propia partición.

**[Mayker]** dimensiones, fijación al cabezal, grado de protección, rango de
temperatura, LEDs, conectores, certificación de zona clasificada y condiciones
de seguridad intrínseca si aplica.
