# ¿Funciona el FET de pull-down de la linea SCCP?
#
# Los 3 slots VACIOS tambien dan retry_rc=1. Con sccpi=1 pasan la primera
# comprobacion de sccp_reset_pulse, luego fallan mas adelante: el candidato
# es PD_LINE_NOT_LOW ("line is actually getting pulled down"), que el driver
# remapea a PD_DETECTION_FAILED y acaba en DISCONTINUE_SCCP=1.
#
# Aqui se acciona sccpo a mano en el puerto 1 (slot VACIO y DESHABILITADO,
# sin fuente de clasificacion en la linea) y se mira si sccpi cae.
#
# SEGURIDAD: solo se toca un GPIO del MCU (P2.17). No se escribe al LTC4296,
# no hay nada insertado en ese slot y el puerto esta deshabilitado. Se deja
# el pin como estaba.
#
# GPIO2 @ 0x4000A000: OUTEN=+0x0C  OUT=+0x18  OUT_SET=+0x1C  OUT_CLR=+0x20  IN=+0x24

proc bit {addr b} {
  return [expr {([lindex [read_memory $addr 32 1] 0] >> $b) & 1}]
}

init
halt

echo [format "reposo      : sccpo1(P2.17)=%d  sccpi1(P2.16)=%d" [bit 0x4000A024 17] [bit 0x4000A024 16]]

# accionar: poner el pin ALTO
mww 0x4000A01C 0x00020000
sleep 5
echo [format "sccpo1 ALTO : sccpo1=%d  sccpi1=%d   <- si sccpi1 cae a 0, el FET conduce" \
      [bit 0x4000A024 17] [bit 0x4000A024 16]]
sleep 20
echo [format "              (recheck) sccpi1=%d" [bit 0x4000A024 16]]

# devolver el pin a como estaba (bajo)
mww 0x4000A020 0x00020000
sleep 5
echo [format "restaurado  : sccpo1=%d  sccpi1=%d" [bit 0x4000A024 17] [bit 0x4000A024 16]]

# lo mismo en el puerto 0, para ver si su linea clavada responde al FET
echo ""
echo [format "puerto0 reposo: sccpo0(P2.15)=%d  sccpi0(P2.14)=%d" [bit 0x4000A024 15] [bit 0x4000A024 14]]
mww 0x4000A01C 0x00008000
sleep 25
echo [format "puerto0 sccpo ALTO: sccpi0=%d" [bit 0x4000A024 14]]
mww 0x4000A020 0x00008000
sleep 5
echo [format "puerto0 restaurado: sccpo0=%d  sccpi0=%d" [bit 0x4000A024 15] [bit 0x4000A024 14]]

resume
shutdown
