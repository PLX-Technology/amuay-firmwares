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
proc lineain {} { return [expr {([lindex [read_memory 0x4000A024 32 1] 0] >> 18) & 1}] }

init
halt
spixfer5 0x68 0xdf 0x01 0x08 0x63
spixfer5 0x66 0xf5 0x20 0x41 0x20
sleep 30
set oe [lindex [read_memory 0x4000A00C 32 1] 0]
echo [format "OUTEN bit20=%d (1=sccpo es salida)" [expr {($oe>>20)&1}]]
set o [lindex [read_memory 0x4000A018 32 1] 0]
echo [format "OUT bit20=%d (0=linea liberada)" [expr {($o>>20)&1}]]
echo [format "IDLE: linea sccpi = %d (esperado 1=alta)" [lineain]]
mww 0x4000A01C 0x100000
sleep 3
echo [format "PULLDOWN: linea sccpi = %d (esperado 0=baja)" [lineain]]
sleep 6
mww 0x4000A020 0x100000
for {set i 0} {$i < 15} {incr i} { echo [format "t%02d presencia: linea=%d" $i [lineain]] }
set d [spixfer5 0x65 0xfc 0x00 0x00 0x00]
echo [format "P2ST=%02x%02x" [lindex $d 2] [lindex $d 3]]
spixfer5 0x66 0xf5 0x00 0x00 0x4e
echo cleanup-ok
resume
shutdown