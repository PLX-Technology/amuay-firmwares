# Igual que mps_sccp_ctrl.tcl pero para el MFS (5 puertos), para comparar
# una placa que SI negocia contra el MPS que no.
#
# La pregunta que responde: ¿en el MFS la linea sccpi reposa ALTA con un
# modulo insertado? Si reposa alta -> la diferencia es real y es del MPS.
# Si reposa BAJA y aun asi negocia -> la premisa "sccpi debe estar alta en
# reposo" es falsa y hay que replantear el diagnostico del MPS entero.
#
# Solo LEE. No escribe nada, ni GPIO ni LTC4296.
#
# GPIO2 @ 0x4000A000: OUTEN=+0x0C  OUT=+0x18  IN=+0x24
# sccpi/sccpo por puerto: p0=P2.14/15  p1=P2.16/17  p2=P2.18/20
#                         p3=P2.21/22  p4=P2.23/24
# Recordatorio del mapeo de slots del MFS: el slot rotulado Port 1 es el
# uplink PDM; los slots PSE Port 2..6 son los puertos LTC 0..4.

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
proc bit {addr b} {
  return [expr {([lindex [read_memory $addr 32 1] 0] >> $b) & 1}]
}

init
halt

# --- control obligatorio: ¿corre la aplicacion? ---
set outen [lindex [read_memory 0x4000A00C 32 1] 0]
echo [format "GPIO2 OUTEN = 0x%08x   (0 = la placa esta en la ROM, la lectura NO vale)" $outen]
echo [format "GPIO2 IN    = 0x%08x" [lindex [read_memory 0x4000A024 32 1] 0]]
echo [format "GPIO2 OUT   = 0x%08x" [lindex [read_memory 0x4000A018 32 1] 0]]
echo ""

echo "slot   puerto  sccpi  sccpo  outen(sccpo)  out(sccpo)"
foreach p {0 1 2 3 4} i {14 16 18 21 23} o {15 17 20 22 24} {
  echo [format "Port%d    p%d      %d      %d         %d             %d" \
        [expr {$p + 2}] $p [bit 0x4000A024 $i] [bit 0x4000A024 $o] \
        [bit 0x4000A00C $o] [bit 0x4000A018 $o]]
}

echo ""
echo "=== LTC4296 ==="
set giost [rd16 {0x0f 0xed 0 0 0}]
echo [format "GCFG   = 0x%04x" [rd16 {0x13 0xb9 0 0 0}]]
echo [format "GFLTEV = 0x%04x" [rd16 {0x05 0xdb 0 0 0}]]
echo [format "GIOST  = 0x%04x   PAD_AUTO(bit3) = %d   PG_OUT0..4 = %d%d%d%d%d" \
      $giost [expr {($giost >> 3) & 1}] \
      [expr {($giost >> 4) & 1}] [expr {($giost >> 5) & 1}] [expr {($giost >> 6) & 1}] \
      [expr {($giost >> 7) & 1}] [expr {($giost >> 8) & 1}]]
echo [format "GCAP   = 0x%04x   NUMPORTS = %d  SCCP_SUPPORT = %d" \
      [set g [rd16 {0x0d 0xe3 0 0 0}]] [expr {$g & 0x1F}] [expr {($g >> 6) & 1}]]
echo [format "P0.16 (AUTO en el MFS): IN=%d OUTEN=%d" [bit 0x40008024 16] [bit 0x4000800C 16]]

echo ""
echo "PxST de los 5 puertos (bits 2:0 = estado PSE; 4 = POWERED):"
foreach p {0 1 2 3 4} f {{0x25 0x3b 0 0 0} {0x45 0x1c 0 0 0} {0x65 0xfc 0 0 0} \
                         {0x85 0x52 0 0 0} {0xa5 0xb2 0 0 0}} {
  set st [rd16 $f]
  echo [format "  P%dST = 0x%04x   estado=%d  POWERED=%d  DET_VLOW=%d DET_VHIGH=%d" \
        $p $st [expr {$st & 0x7}] [expr {($st >> 4) & 1}] \
        [expr {($st >> 12) & 1}] [expr {($st >> 13) & 1}]]
}

echo ""
echo "muestreo sccpi (15 x 100 ms):"
for {set i 0} {$i < 15} {incr i} {
  echo [format "  t%02d  p0=%d p1=%d p2=%d p3=%d p4=%d" $i \
        [bit 0x4000A024 14] [bit 0x4000A024 16] [bit 0x4000A024 18] \
        [bit 0x4000A024 21] [bit 0x4000A024 23]]
  sleep 100
}
resume
shutdown
