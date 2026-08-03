# Corriente que el MFS entrega por su puerto LTC 0 (slot Port 2).
#
# POR QUE
# Saber si la ATT consume de verdad, independientemente del USB. El estado
# "entregando" (PxST=0x3e12) dice que el PSE tiene la salida activa, NO que
# haya alguien tirando corriente al otro lado.
#
# LECTURA (ltc4296.c:421-444)
#   bit 12 (NEW_MSK) = 1 -> el dato vale; si es 0 NO hay medida, y un 0 de
#                           corriente ahi seria un dato falso muy creible.
#   i_mA = ((val & 0x0FFF) - 2048) * 1000 / (10 * hs_resistor)
#   hs_resistor del MFS: el overlay dice 250 mohm (medido 240 -> ~4% de error)
#
# PEC generados con gen_frames.py, que se autovalida contra frames conocidos.
# Solo LEE.

proc ltcread {reg pecb name} {
  mww 0x40046008 0x00050005
  mww 0x4004601C 0xC000C0
  mww 0x40046020 0xFFFFFFFF
  mwb 0x40046000 [expr {($reg << 1) | 1}]
  mwb 0x40046000 $pecb
  mwb 0x40046000 0
  mwb 0x40046000 0
  mwb 0x40046000 0
  set c [lindex [read_memory 0x40046004 32 1] 0]
  mww 0x40046004 [expr {($c & ~0xF0000) | 0x20000 | 0x20 | 0x1}]
  sleep 3
  set d [read_memory 0x40046000 8 5]
  set v [expr {([lindex $d 2] << 8) | [lindex $d 3]}]
  echo [format "%s = %04x" $name $v]
  return $v
}

init
halt

set st [ltcread 0x12 0x3b P0ST]
echo [format "  estado PSE = %d  (2 = entregando)" [expr {$st & 0x7}]]

set a [ltcread 0x16 0x03 P0ADCDAT]
if {($a & 0x1000) == 0} {
  echo "  >>> bit NEW = 0: SIN MEDIDA VALIDA (no es cero amperios, es 'no se')"
} else {
  set ma [expr {(($a & 0x0FFF) - 2048) * 1000 / (10 * 250)}]
  echo [format "  corriente = %d mA  (codigo %d)" $ma [expr {$a & 0x0FFF}]]
}

resume
shutdown
