# Corriente por puerto del LTC4296 del MPS, EN CRUDO.
#
# Objetivo: fijar la escala de conversion a mA. El driver hace
#     i_mA = (codigo - 2048) * 1000 / (10 * hs_resistor)
# y el devicetree dice hs-resistor = 250, pero no consta la UNIDAD:
#     miliohmios -> 250 = 0.25 ohm  (sensores reales del MPS: 0.27)  -> 0.37 mA/LSB
#     centiohmios ->  27 = 0.27 ohm                                  -> 3.7  mA/LSB
# Un factor de 10 entre ambas. Por eso aqui se saca el CODIGO CRUDO: se compara
# con la corriente real medida en la fuente y la escala sale sola.
#
# PxADCDAT = (puerto+1)*0x10 + 6.  bit12 = NEW (dato fresco), bits 11:0 = codigo.
#
# Solo LEE.

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

proc fila {p adc st} {
  set a [rd16 $adc]
  set s [rd16 $st]
  set cod [expr {$a & 0xFFF}]
  set new [expr {($a >> 12) & 1}]
  set d   [expr {$cod - 2048}]
  echo [format "  p%d  PxST=0x%04x %-11s  ADCDAT=0x%04x NEW=%d  codigo=%4d  delta=%5d   ->  %6.2f mA (si 0.37/LSB)   %7.1f mA (si 3.7/LSB)" \
        $p $s [expr {($s & 0x7) == 2 ? "ENTREGANDO" : "(no entrega)"}] \
        $a $new $cod $d [expr {$d * 0.370}] [expr {$d * 3.704}]]
}

init
halt

set outen [lindex [read_memory 0x4000A00C 32 1] 0]
echo [format "GPIO2 OUTEN = 0x%08x  (0 = ROM, la lectura NO vale)" $outen]
if {$outen == 0} { echo "ABORTADO: la placa esta en la ROM."; resume; shutdown }

echo [format "GCFG = 0x%04x   GFLTEV = 0x%04x" [rd16 {0x13 0xb9 0 0 0}] [rd16 {0x05 0xdb 0 0 0}]]
echo ""
echo "corriente por puerto (codigo crudo del ADC):"
fila 0 {0x2d 0x03 0 0 0} {0x25 0x3b 0 0 0}
fila 1 {0x4d 0x24 0 0 0} {0x45 0x1c 0 0 0}
fila 2 {0x6d 0xc4 0 0 0} {0x65 0xfc 0 0 0}
fila 3 {0x8d 0x6a 0 0 0} {0x85 0x52 0 0 0}

echo ""
echo "repeticion (8 x 500 ms), para ver estabilidad y ruido:"
for {set i 0} {$i < 8} {incr i} {
  set a0 [rd16 {0x2d 0x03 0 0 0}]
  set a3 [rd16 {0x8d 0x6a 0 0 0}]
  echo [format "  t%d  p0 codigo=%4d   p3 codigo=%4d" $i [expr {$a0 & 0xFFF}] [expr {$a3 & 0xFFF}]]
  sleep 500
}
resume
shutdown
