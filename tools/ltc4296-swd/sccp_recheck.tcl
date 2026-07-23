proc lineain {} { return [expr {([lindex [read_memory 0x4000A024 32 1] 0] >> 18) & 1}] }
init
halt
echo [format "sccpi puerto2 (PSM solo, sin cable) = %d" [lineain]]
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
set d [spixfer5 0x65 0xfc 0 0 0]
echo [format "P2ST=%02x%02x" [lindex $d 2] [lindex $d 3]]
set d [spixfer5 0x67 0xf2 0 0 0]
echo [format "P2CFG0=%02x%02x" [lindex $d 2] [lindex $d 3]]
resume
shutdown