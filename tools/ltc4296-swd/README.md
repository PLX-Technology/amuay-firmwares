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

Uso:

```
openocd -s <scripts> -c "adapter driver cmsis-dap" -c "adapter speed 1000" -f target/max32690.cfg -c "reset_config none" -f <script>.tcl
```

Los scripts están hardcodeados para el puerto 2 (slot Port 4) del FSW; para
otro puerto regenerar los frames (PEC) con el algoritmo del driver (el
generador Python vive en la sesión; `prebuilt/patch-class11.py` muestra el
mismo PEC).

Datos clave del mapeo (esquemático MFS):
- SPI del LTC: SPI0 @ 0x40046000, SS1 (P2.26), 500 kHz, modo 3
- SCCP por puerto (sccpi/sccpo): p0=P2.14/P2.15, p1=P2.16/P2.17,
  p2=P2.18/P2.20, p3=P2.21/P2.22 (sccpo ACTIVE_LOW = gate de FET pull-down)
- GPIO2 @ 0x4000A000 (IN=+0x24, OUT_SET=+0x1C, OUT_CLR=+0x20)
- Slots: Port 1 = uplink PDM (PSE_OUT → rail); Port 2..6 = PSE P0..P4;
  el firmware solo instancia los puertos LTC 0-3 (slots Port 2-5)
