# Módulos SPE — PSM y PDM

Ficha técnica y procedimiento de instalación.
Rev. 2026-08-11 · Sistema de monitoreo de tanques Varec

> ⚠️ Los campos marcados **[Mayker]** están pendientes de confirmar con
> fabricación: mecánica, referencias de conector, colores de LED y rangos
> ambientales. Todo lo demás está medido o verificado en banco.

---

## 1. Qué son

Módulos enchufables que dan **un puerto 10BASE-T1L (par único, SPE)** a un slot
del switch. Cada módulo lleva su propio PHY **ADIN1100**.

| | **PSM** | **PDM** |
|---|---|---|
| Papel | **entrega** potencia (PSE) | **recibe** potencia (PD) |
| Dónde va | slots de salida del MPS/MFS | slot de **uplink** (Port 1) del MFS |
| Alimenta a | un sensor ATT o un field switch | *(alimenta la placa donde va montado)* |

**Regla práctica:** un field switch lleva **un PDM** en su Port 1 —por donde
recibe los 50 V y los datos del padre— y **PSM** en los slots que use.

---

## 2. El DIP de 4 posiciones

Cada módulo lleva un DIP (CVS-04B) con resistencias de 4,7 kΩ a VDD3P3.

| Posición | Función |
|---|---|
| `PHYAD_2`, `PHYAD_1`, `PHYAD_0` | Dirección MDIO del PHY |
| **`RXD0`** | **Habilita el nivel de transmisión de 2,4 Vpp** |

### 2.1 ★ La dirección MDIO tiene que coincidir con el slot

**El strap = el número de macPort de DATOS del slot**, que **no** coincide con
la serigrafía. Ver la tabla en la ficha del MFS.

⚠️ **Un strap equivocado no se nota al enchufar.** El enlace puede entrenar
igual —el ADIN1100 linkea con sus valores por defecto— pero el switch **no ve
el PHY** y **no cruza ni una trama**. Parece un problema de red y es un
interruptor mal puesto.

### 2.2 ★★ `RXD0` ABIERTO en todo tramo largo

| `RXD0` | Nivel | Alcance |
|---|---|---|
| Cerrado (de fábrica) | 1,0 Vpp | ~200 m |
| **Abierto** | **2,4 Vpp** | **hasta 1000 m** |

Abrirlo habilita la **capacidad real** del PHY, y de ahí sale todo en cascada:
el anuncio de autonegociación, la petición y el nivel acordado. **No hay
firmware que lo supla.**

⚠️ **Hay que abrirlo en LOS DOS EXTREMOS del tramo**, incluido el **PDM** del
field switch que recibe el cable. El enlace sube a 2,4 V solo si ambos extremos
son capaces; con uno solo, la negociación cae a 1,0 V y los 1000 m no enlazan.

Para tiradas cortas (dentro de una caseta, cabezal a switch) da igual, pero
**dejarlo abierto siempre es lo más seguro**: contra un extremo que no puede,
la negociación cae sola a 1,0 V y el enlace sube igual.

---

## 3. Verificación en campo

Con el switch alimentado a 50 V y el módulo puesto:

| Qué se observa | Significa |
|---|---|
| El puerto entrega potencia y el equipo del otro extremo arranca | La parte de potencia está bien |
| El equipo aparece **en la pasarela** | Los datos cruzan: el strap MDIO es correcto |
| Entrega potencia pero **no aparece nada** en la pasarela | Sospechar del **strap MDIO** o del nivel de 2,4 V si el tramo es largo |

⚠️ **Que haya enlace NO prueba que pasen datos.** Es el error de diagnóstico
más caro de este sistema: se ve el LED, se da por bueno el montaje y el fallo
está en un interruptor.

---

## 4. Fallos conocidos y cómo se ven

**Módulo en cortocircuito en su par.** El puerto queda en detección
permanente y **nunca** entrega. Se distingue midiendo la tensión de sondeo:
un módulo sano da 4,0–4,5 V durante la clasificación y uno en corto **~70 mV**.
Es un fallo de hardware del módulo, no del switch. *(Visto en un PSM.)*

**Módulo que no declara 2,4 V ni con el DIP abierto.** Con `RXD0` abierto, sus
hermanos declaran la capacidad y él no. Es defecto del módulo — interruptor sin
contacto, resistencia mal soldada o el propio PHY. *(Visto en un módulo del
field switch `881e`, slot Port 5.)*

**Par invertido en el zócalo.** Si el par llega con la polaridad cambiada, el
módulo no negocia potencia. *(Visto en el MPS.)* **[Mayker]** confirmar el
marcado de polaridad del zócalo.

---

## 5. Datos eléctricos

| Parámetro | Valor |
|---|---|
| Norma | IEEE 802.3cg **10BASE-T1L**, par único |
| Velocidad | 10 Mbit/s |
| Nivel de transmisión | 1,0 Vpp o **2,4 Vpp** (según `RXD0`) |
| Alcance | ~200 m a 1,0 V · **hasta 1000 m a 2,4 V**, con cable apto |
| Potencia | SPoE, **clase 13** — exige 50 a 58 V en el switch |
| Impedancia de línea esperada | 100 Ω |

**[Mayker]** rango de temperatura, dimensiones, referencia del conector,
certificación de zona clasificada.

---

## 6. ⚠️ Antes de instalar: el cable

Los 1000 m del estándar suponen **par trenzado especificado**. Cable de
instrumentación de conductores sueltos **no sirve**, por grueso que sea.

**Especificación mínima:** par trenzado individual · 18 AWG · **100 Ω** ·
capacitancia mutua **≤ 45 nF/km** · apantallado · vendido como **10BASE-T1L /
SPE** o **fieldbus Type A (IEC 61158-2)**.

⚠️ **Los dos hilos, del MISMO par trenzado.** Un par partido —un hilo de cada
par— tiene continuidad perfecta y transmisión nula, y a 80 m incluso funciona.

⚠️ **Nada a través del par.** Terminadores de bus de campo, descargadores o
cualquier condensador entre los dos hilos dejan pasar la continua y se comen la
señal: la placa del otro extremo **arranca** y no hay datos. Revisar cajas de
empalme del recorrido.

Detalle completo y procedimiento de aceptación de un carrete en
[`field-switch/ENLACES-LARGOS-2V4.md`](../../field-switch/ENLACES-LARGOS-2V4.md).
