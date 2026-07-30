# ¿Esta corriendo la aplicacion, o el chip se quedo en la ROM?
# App = 0x10000000.  ROM = 0x0000xxxx.
# Muestrea el PC varias veces: si se mueve dentro de la ROM, esta en un bucle
# del bootloader; si no se mueve, esta colgado. Solo lee.
init
for {set i 0} {$i < 6} {incr i} {
  halt
  echo [format "  muestra %d  pc = 0x%08x" $i [reg pc force]]
  resume
  sleep 300
}
halt
echo [format "GPIO2 OUTEN = 0x%08x   (0x00528000 = firmware arrancado)" \
      [lindex [read_memory 0x4000A00C 32 1] 0]]
resume
shutdown
