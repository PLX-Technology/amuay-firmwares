# Fichas de placa — monitoreo de tanques Varec

Ficha técnica y procedimiento de instalación de cada placa, para el equipo que
monta en refinería.

| Ficha | Qué cubre |
|---|---|
| [MPS — Power switch](MPS-power-switch.md) | Cabecera: reparte 50 V y datos a los field switches. 4 slots |
| [MFS — Field switch](MFS-field-switch.md) | Reparte a sensores; se encadena. 6 slots, uplink por PDM |
| [ATT — Sensor Varec 2500](ATT-sensor-varec.md) | Encoder en el cabezal del tanque |
| [Módulos PSM y PDM](MODULOS-PSM-PDM.md) | Los módulos SPE de cada slot: straps y DIP |

> **[Mayker]** marca lo pendiente de fabricación: mecánica, conectores, LEDs,
> rangos ambientales y certificaciones. Todo lo demás está medido o verificado
> en banco.

---

## Las cuatro cosas que más caro cuestan en campo

**1. El strap MDIO de cada módulo tiene que coincidir con el slot**, y la
serigrafía **no** coincide con la numeración interna. Un strap equivocado deja
el enlace entrenando y **sin cruzar una sola trama**: parece un problema de red
y es un interruptor.

**2. Que un puerto entregue potencia NO significa que pasen datos.** Es el
error de diagnóstico más caro del sistema. La comprobación buena es que el
equipo **aparezca en la pasarela**.

**3. Los tramos largos necesitan el DIP `RXD0` abierto en LOS DOS extremos.**
Sin eso el enlace negocia 1,0 Vpp, que llega a unos 200 m. Con 2,4 Vpp llega a
1000 m — pero solo si ambos extremos son capaces.

**4. Y el cable manda por encima de todo.** Cable de instrumentación de
conductores sueltos **no sirve** a 1000 m por grueso que sea: la continua pasa,
la señal no. Hace falta par trenzado de 100 Ω y ≤ 45 nF/km. Antes de comprar
kilómetros, **probar un carrete** — el procedimiento son diez minutos y está en
[`field-switch/ENLACES-LARGOS-2V4.md`](../../field-switch/ENLACES-LARGOS-2V4.md).

---

## Orden de trabajo recomendado

1. **En el banco**, antes de salir: grabar el firmware de cada placa, poner los
   straps y abrir los DIP que toquen. Es la única visita obligatoria de cada
   equipo; a partir de ahí todo se actualiza por SPE desde la pasarela.
2. **En campo**: montar, cablear, alimentar desde la cabecera.
3. **Verificar en la pasarela**, equipo por equipo, antes de dar por buena la
   instalación de cada tramo.
4. **Calibrar** cada tanque por geometría, que no exige mover producto.
