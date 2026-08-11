# MFS — Field switch

Ficha técnica y procedimiento de instalación.
Rev. 2026-08-11 · Sistema de monitoreo de tanques Varec

> ⚠️ **[Mayker]** = pendiente de fabricación (mecánica, conectores, LEDs,
> rangos ambientales). El resto está medido o verificado en banco.

---

## 1. Qué hace

Switch de campo de **6 slots SPE**. Recibe potencia y datos del MPS por su
**uplink** y los reparte a sensores ATT — o a **otro field switch**, que puede
encadenarse.

| | |
|---|---|
| Conmutador | **ADIN6310** (6 puertos 10BASE-T1L) |
| Microcontrolador | **MAX32690** (Cortex-M4), firmware Zephyr |
| Controlador PSE | **LTC4296** (SPoE clase 13) |
| Alimentación | **50 a 58 V** por SPE, a través de su PDM |
| Identidad | MAC derivada del **USN** del micro — única y estable |

---

## 2. ★ Mapa de slots

**La serigrafía no coincide con la numeración interna.** Esta tabla es la que
manda:

| Slot (serigrafía) | Conector | Puerto de **datos** (strap MDIO del módulo) | Puerto de **potencia** |
|---|---|---|---|
| **Port 1** | J1 | **2** | *(uplink: recibe PoDL, no entrega)* |
| **Port 2** | J3 | **1** | LTC 0 |
| **Port 3** | J4 | **0** | LTC 1 |
| **Port 4** | J5 | **5** | LTC 2 |
| **Port 5** | J6 | **4** | LTC 3 |
| **Port 6** | J8 | **3** | LTC 4 |

⚠️ **El número de datos y el de potencia NO coinciden en el mismo slot.**
Ejemplo: el slot *Port 4* es datos `macPort5` y potencia `LTC 2`. Al montar un
módulo hay que poner **el strap de la columna de datos**.

**Port 1 es el uplink y lleva PDM**, no PSM: es por donde entra la
alimentación. Los demás llevan PSM.

---

## 3. Consumo — medido 2026-08-11

| Situación | Corriente | Potencia |
|---|---|---|
| Switch solo, **1 puerto** energizado | 18,8 mA | 1,00 W |
| Switch solo, **4 puertos** energizados | 32,0 mA | 1,79 W |
| Coste aproximado por puerto energizado | ~4,4 mA | ~0,23 W |

**Para dimensionar:** un field switch con 5 sensores ronda los **95 mA / 5 W**
vistos desde su padre (switch + puertos + sensores).

Medido a 53 V. La potencia es estimada —corriente por la tensión de entrada—
así que sirve para dimensionar, no para un balance fino.

---

## 4. Instalación

### 4.1 Antes de ir a campo

1. **Grabar el firmware por cable, en el banco.** Es la única visita
   obligatoria de la placa: después se actualiza por SPE desde la pasarela.
   Ver [`field-switch/GRABADO-POR-SERIE.md`](../../field-switch/GRABADO-POR-SERIE.md).
2. **Comprobar que el ADIN6310 está aprovisionado.** Una placa nueva lo trae en
   blanco y el firmware de producción no consigue cargárselo: arranca pero
   ningún puerto funciona. Ver la nota de aprovisionamiento.
3. **Poner los straps MDIO** de cada módulo según la tabla del punto 2.
4. **Abrir el DIP `RXD0`** de todos los módulos que vayan a un tramo largo —
   **incluido el PDM del uplink**.

### 4.2 En campo

1. Montar el **PDM en el Port 1** y los **PSM** en los slots que se usen.
2. Conectar el cable del padre (MPS u otro MFS) al **Port 1**.
3. Conectar los sensores a los slots de salida.
4. Alimentar el padre. El switch arranca cuando el padre negocia y entrega.

⚠️ **No se alimenta con fuente externa en servicio.** El switch se alimenta por
SPE desde su padre. La fuente de banco se usa **solo** para grabar firmware,
porque el padre sondea el puerto periódicamente y cortaría la sesión.

### 4.3 Verificación

| Comprobación | Dónde se ve |
|---|---|
| El switch aparece con su MAC única | Pasarela, sección de equipos |
| Entrega potencia en los slots ocupados | Pasarela, consumo por slot |
| Cada sensor conectado aparece en línea | Pasarela, tabla de tanques |

⚠️ **Que un puerto entregue potencia no significa que pasen datos.** Si
entrega y el equipo del otro extremo **no aparece** en la pasarela, sospechar
del strap MDIO del módulo o —en tramos largos— del nivel de 2,4 V.

---

## 5. Fallos conocidos

| Síntoma | Causa probable |
|---|---|
| Arranca pero **ningún puerto funciona** | ADIN6310 sin aprovisionar (placa nueva) |
| Un puerto entrega y el equipo no aparece en la pasarela | Strap MDIO del módulo equivocado |
| Un puerto **nunca** entrega, sondeo ~70 mV | Módulo en cortocircuito |
| Un tramo largo no enlaza y sí lo hace con cable corto | `RXD0` cerrado en algún extremo, o **el cable no es apto** |
| Un puerto queda en estado **inactivo** y no reintenta | Estado muerto del LTC4296; el firmware actual ya lo evita |
| LED de alimentación encendido pero **sin consola ni respuesta** | *(En investigación con Mayker: visto en tres placas nuevas.)* |

---

## 6. Límites y avisos

- **El firmware NUNCA entrega potencia sin negociar.** No existe forzado
  manual, y así debe seguir: sin negociación se han quemado dos LTC4296.
- Los slots **Port 2 a Port 6** pueden entregar; el **Port 1 no** (es entrada).
- A menos de 50 V el LTC4296 **se niega a energizar** y lo dice por consola.
  Es correcto: la clase 13 exige esa tensión.
- **Actualización remota:** por SPE desde la pasarela, sin visita.

**[Mayker]** dimensiones, fijación, grado de protección, rango de temperatura,
significado de cada LED, referencia de conectores, certificación de zona.
