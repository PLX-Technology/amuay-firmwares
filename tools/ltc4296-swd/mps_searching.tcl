# ¿Cuanto tarda el puerto en llegar a SEARCHING tras la clasificacion?
#
# do_spoe_sccp espera SOLO 4 ms y luego exige (PxST & 0x7) == 3 (SEARCHING).
# Si no lo esta, cae fuera del if y devuelve DISCONTINUE_SCCP (rc = 1) sin
# llegar a hablar SCCP nunca.
#
# En la medida manual anterior el puerto SI estaba en SEARCHING (PxST=0x2023),
# pero leido a los 30 ms. Aqui se lee lo antes posible y de forma consecutiva
# para ver la progresion.
#
# Resolucion: cada transaccion SPI lleva un `sleep 2` dentro, asi que cada
# lectura son ~3-5 ms. Suficiente para distinguir "ya esta a la primera" de
# "tarda varias decenas de ms".
#
# Puerto 1 = slot 2, que es donde esta el modulo.
#
# SEGURIDAD: prebias + clasificacion (corriente limitada), lo mismo que el
# firmware hace solo cada ~5 s. Deshabilita el puerto al salir.

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
proc nombre {e} {
  switch $e {
    0 {return "DISABLED"} 1 {return "SLEEPING"} 2 {return "DELIVERING"}
    3 {return "SEARCHING <<<"} 4 {return "ERROR"} 5 {return "IDLE"}
    6 {return "PREPDET"} default {return "UNKNOWN"}
  }
}

init
halt
echo [format "GPIO2 OUTEN = 0x%08x" [lindex [read_memory 0x4000A00C 32 1] 0]]
echo [format "P1ST de partida = 0x%04x" [rd16 {0x45 0x1c 0 0 0}]]
echo ""
echo "prebias + clasificacion en el puerto 1, y lecturas consecutivas:"

spixfer5 0x48 0x3f 0x01 0x08 0x63     ;# P1CFG1 = 0x0108
spixfer5 0x46 0x15 0x20 0x41 0x20     ;# P1CFG0 = 0x2041
for {set i 0} {$i < 14} {incr i} {
  set st [rd16 {0x45 0x1c 0 0 0}]
  echo [format "  lectura %2d  P1ST = 0x%04x  estado=%d %-14s  sccpi1=%d" \
        $i $st [expr {$st & 0x7}] [nombre [expr {$st & 0x7}]] \
        [expr {([lindex [read_memory 0x4000A024 32 1] 0] >> 16) & 1}]]
}
echo ""
echo "y tras 200 ms mas:"
sleep 200
set st [rd16 {0x45 0x1c 0 0 0}]
echo [format "  P1ST = 0x%04x  estado=%d %s" $st [expr {$st & 0x7}] [nombre [expr {$st & 0x7}]]]

spixfer5 0x46 0x15 0x00 0x00 0x4e     ;# deshabilitar
resume
shutdown
