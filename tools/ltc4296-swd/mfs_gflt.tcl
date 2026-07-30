# ¿El UVLO_DIGITAL del MFS es un latch rancio del POR, o se esta reafirmando?
#
# GFLTEV = 0x0010 (bit4 = UVLO_DIGITAL) en el MFS, contra 0x0000 en el MPS.
# El MPS lo limpia porque su firmware llama a clear_global_faults al arrancar
# (main.c:617). La build vieja del MFS parece no hacerlo.
#
# Se limpia el latch (GFLTEV = 0x001F) y se relee varias veces:
#   queda en 0        -> era un latch rancio del arranque, nadie lo limpio
#   vuelve a 0x0010   -> hay un UVLO real y recurrente (problema de fuente)
#
# SEGURIDAD: escribir GFLTEV solo BORRA latches de fallo. No habilita ningun
# puerto, no toca PxCFG0/PxCFG1, no llama a force_port_pwr. Es exactamente lo
# que el firmware del MPS hace en cada arranque y en cada reintento.
# NO se toca clear_ckt_breaker (ese re-arma el breaker del lado bajo).

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
proc estado {t} {
  echo [format "  %-14s GFLTEV=0x%04x  P0ST=0x%04x  P2ST=0x%04x" $t \
        [rd16 {0x05 0xdb 0 0 0}] [rd16 {0x25 0x3b 0 0 0}] [rd16 {0x65 0xfc 0 0 0}]]
}

init
halt
echo [format "GPIO2 OUTEN = 0x%08x  (control: la app tiene que estar corriendo)" \
      [lindex [read_memory 0x4000A00C 32 1] 0]]
estado "antes"
spixfer5 0x04 0xdc 0x00 0x1f 0x13     ;# GFLTEV = 0x001F  limpiar latches
sleep 20
estado "tras limpiar"
sleep 200
estado "+200 ms"
sleep 500
estado "+700 ms"
sleep 1000
estado "+1.7 s"
resume
shutdown
