# Que lee el LTC4296 en su pin AUTO (pin 13), y capacidades del chip.
# En el MPS ese pin va DIRECTO A GND; en el MFS lleva pull-down y lo puede
# manejar P0.16 del micro. Solo LEE registros globales.
#
# GIOST bit3 = PAD_AUTO. AUTO bajo = modo GESTIONADO (el chip no energiza
# solo, espera al host por SPI) = lo que el firmware da por supuesto.
# GCAP bit6 = SCCP_SUPPORT.

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

set giost [rd16 {0x0f 0xed 0 0 0}]
set gcap  [rd16 {0x0d 0xe3 0 0 0}]
echo [format "GIOST = 0x%04x   PAD_AUTO(bit3) = %d   <- 0 = modo GESTIONADO" \
      $giost [expr {($giost >> 3) & 1}]]
echo [format "        PAD_WAKEUP(bit2)=%d  PG_OUT0..3 = %d%d%d%d" \
      [expr {($giost >> 2) & 1}] [expr {($giost >> 4) & 1}] [expr {($giost >> 5) & 1}] \
      [expr {($giost >> 6) & 1}] [expr {($giost >> 7) & 1}]]
echo [format "GCAP  = 0x%04x   SCCP_SUPPORT(bit6) = %d   WAKE_FWD(bit5) = %d" \
      $gcap [expr {($gcap >> 6) & 1}] [expr {($gcap >> 5) & 1}]]
echo ""
echo [format "sccpi ahora: p0=%d p1=%d p2=%d p3=%d" \
      [bit 0x4000A024 14] [bit 0x4000A024 16] [bit 0x4000A024 18] [bit 0x4000A024 21]]
echo [format "P0.16 (AUTO en el MFS): IN=%d OUTEN=%d" \
      [bit 0x40008024 16] [bit 0x4000800C 16]]
resume
shutdown
