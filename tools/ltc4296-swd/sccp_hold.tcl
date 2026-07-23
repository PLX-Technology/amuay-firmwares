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
init
halt
spixfer5 0x68 0xdf 0x01 0x08 0x63
spixfer5 0x66 0xf5 0x20 0x41 0x20
sleep 30
set g [lindex [read_memory 0x4000A024 32 1] 0]
echo [format "puerto 2 EN CLASIFICACION (retenido) - sccpi=%d" [expr {($g>>18)&1}]]
resume
shutdown