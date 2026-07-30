# CONTROL de polaridad: retiene el puerto 0 del MFS (slot Port 2) en
# clasificacion 120 s, para medir con el multimetro EXACTAMENTE en los mismos
# puntos y con las mismas puntas que en el MPS.
#
# En el MPS (mps_hold_clasif.tcl) se midio -5 V: la tension de clasificacion
# LLEGA al conector, pero con el par al reves. Falta saber si eso es un
# defecto del MPS o simplemente como estan nombrados los pines.
#
#   MFS +5 V y MPS -5 V con las puntas igual  -> inversion REAL, y es del MPS
#   los dos -5 V                              -> son las puntas, no la placa
#
# El puerto 0 del MFS es uno de los que SI negocia (sccpi sube a 1 en
# SEARCHING), asi que es la referencia buena.
#
# SEGURIDAD: prebias + clasificacion, corriente limitada. Nunca
# SW_POWER_AVAILABLE. El puerto queda deshabilitado al salir.

proc spixfer5 {b0 b1 b2 b3 b4} {
  mww 0x40046008 0x00050005
  mww 0x4004601C 0xC000C0
  mww 0x40046020 0xFFFFFFFF
  mwb 0x40046000 $b0
  mwb 0x40046000 $b1
  mwb 0x40046000 $b2
  mwb 0x40046000 $b3
  mwb 0x40046000 $b4
  set c [lindex [read_memory 0x40046004 32 1] 0]
  mww 0x40046004 [expr {($c & ~0xF0000) | 0x20000 | 0x20 | 0x1}]
  sleep 2
  return [read_memory 0x40046000 8 5]
}
proc rd16 {frame} {
  set d [eval spixfer5 $frame]
  return [expr {([lindex $d 2] << 8) | [lindex $d 3]}]
}

init
halt

set outen [lindex [read_memory 0x4000A00C 32 1] 0]
echo [format "GPIO2 OUTEN = 0x%08x" $outen]
if {$outen == 0} {
  echo ""
  echo "ABORTADO: la placa esta en la ROM, no en la aplicacion."
  echo "  -> quitar la cinta SWD, POR de 10 s, esperar unos segundos a que"
  echo "     termine el arranque seguro, y reconectar."
  resume
  shutdown
}

spixfer5 0x28 0x18 0x01 0x08 0x63     ;# P0CFG1 = 0x0108
spixfer5 0x26 0x32 0x20 0x41 0x20     ;# P0CFG0 = 0x2041
sleep 50
echo ""
echo ">>> MIDE AHORA en el slot Port 2 del MFS, en los MISMOS puntos y con"
echo ">>> las MISMAS puntas que usaste en el MPS.   (120 s)"
echo ""
for {set i 0} {$i < 60} {incr i} {
  sleep 2000
  set st [rd16 {0x25 0x3b 0 0 0}]
  echo [format "  %3ds  P0ST=0x%04x  estado=%d %s  sccpi0=%d" \
        [expr {($i + 1) * 2}] $st [expr {$st & 0x7}] \
        [expr {($st & 0x7) == 3 ? "SEARCHING" : "         "}] \
        [expr {([lindex [read_memory 0x4000A024 32 1] 0] >> 14) & 1}]]
}

spixfer5 0x26 0x32 0x00 0x00 0x4e     ;# deshabilitar
echo "puerto 0 deshabilitado"
resume
shutdown
