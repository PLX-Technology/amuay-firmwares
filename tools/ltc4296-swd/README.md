# Diagnóstico LTC4296 por SWD (sin consola, sin recompilar)

Scripts openocd (Tcl) que manejan el SPI0 (0x40046000, SS1) y GPIO2 del
MAX32690 a registro pelado para leer/escribir el LTC4296 del field switch
con el firmware corriendo (halt durante la transacción). Frame de 5 bytes
con PEC CRC8 seed 0x41 (ver driver ltc4296.c).

- `ltc_dump.tcl` — volcado de registros globales + P0-P3 (estado/config)
- `ltc_replay.tcl` — replay manual de prebias+clasificación cronometrando PxST
- `sccp_probe.tcl` — pulso de reset SCCP manual por GPIO + búsqueda de presencia
- `sccp_ab.tcl` — A/B línea SCCP puerto cargado vs slot vacío
- `sccp_hold.tcl` — deja un puerto retenido en clasificación para medir en banco
- `sccp_recheck.tcl` — relee línea sccpi + PxST/PxCFG0

Para el **MPS** (mismo SPI0/SS1 y mismo pinout SCCP que el MFS):

- `gen_frames.py` — **generador de frames**. Emite los 5 bytes con PEC para
  cualquier puerto y se **autovalida** reproduciendo los frames conocidos de
  `vout_p3.tcl`/`gadc_vout.tcl` antes de imprimir nada. Usarlo siempre en vez
  de calcular el PEC a mano.
- `mps_vout_ab.tcl` — A/B de la **tensión de sondeo**: puerto 0 (cargado)
  contra puerto 1 (vacío). Es la medida que distingue un par en corto
  (35-70 mV) de uno sano/abierto (~5145 mV); `PxST` y `retry_rc` **no** los
  distinguen.
- `mps_pd_fet.tcl` — acciona `sccpo` a mano para ver si el FET de pull-down
  de la linea SCCP responde (el FET vive en el modulo PSM, no en la placa).
- `mps_sccp_ctrl.tcl` — **solo lectura**: dirección y nivel de los pines
  `sccpi`/`sccpo` de los 4 puertos. Control obligatorio antes de culpar al
  módulo: comprueba que `sccpi` bajo no lo esté causando nuestro propio FET.

Uso:

```
openocd -s <scripts> -c "adapter driver cmsis-dap" -c "adapter speed 1000" -f target/max32690.cfg -c "reset_config none" -f <script>.tcl
```

Los scripts `sccp_*`/`ltc_*` están hardcodeados para el puerto 2 (slot Port 4)
del FSW; para otro puerto regenerar los frames con `gen_frames.py`.

Mapa de registros (confirmado en `ltc4296.h`, no inferido):
`PxEV/PxST/PxCFG0/PxCFG1 = (puerto+1)*0x10 + {0,2,3,4}`, o sea
P0 = 0x10/0x12/0x13/0x14 … P4 = 0x50/0x52/0x53/0x54.
`GADCCFG = 0x0A` con valor `0x40 | set_port_vout[p]`, donde
`set_port_vout = {0x04,0x06,0x08,0x0A,0x0C}`. `GADCDAT = 0x0B`,
mV = `(código - 2048) * 35`.

**Seguridad al usar estos scripts:** escribir `PxCFG1=0x0108` (prebias) y
`PxCFG0=0x2041` (`SET_CLASSIFICATION_MODE|SW_PSE_READY|SW_EN`) es modo
clasificación, corriente limitada, y es byte a byte lo que el firmware ya
hace solo cada ~5 s en `retry_spoe_sccp`. Lo que **nunca** hay que poner es
`SW_POWER_AVAILABLE` (BIT5 de PxCFG0) ni llamar a `force_port_pwr`: eso
energiza sin negociar. Dejar siempre `PxCFG0=0x0000` al salir.

Datos clave del mapeo (esquemático MFS):
- SPI del LTC: SPI0 @ 0x40046000, SS1 (P2.26), 500 kHz, modo 3
- SCCP por puerto (sccpi/sccpo): p0=P2.14/P2.15, p1=P2.16/P2.17,
  p2=P2.18/P2.20, p3=P2.21/P2.22 (sccpo ACTIVE_LOW = gate de FET pull-down)
- GPIO2 @ 0x4000A000 (IN=+0x24, OUT_SET=+0x1C, OUT_CLR=+0x20)
- Slots: Port 1 = uplink PDM (PSE_OUT → rail); Port 2..6 = PSE P0..P4;
  el firmware solo instancia los puertos LTC 0-3 (slots Port 2-5)
