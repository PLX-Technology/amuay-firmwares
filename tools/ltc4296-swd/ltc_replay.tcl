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
echo "prebias escrito (P2CFG1=0x0108)"
spixfer5 0x66 0xf5 0x20 0x41 0x20
echo "class-enable escrito (P2CFG0=0x2041) - cronometrando P2ST"
for {set i 0} {$i < 40} {incr i} {
  set d [spixfer5 0x65 0xfc 0 0 0]
  echo [format "t%02d P2ST=%02x%02x" $i [lindex $d 2] [lindex $d 3]]
}
set d [spixfer5 0x61 0xe0 0 0 0]
echo [format "P2EV=%02x%02x" [lindex $d 2] [lindex $d 3]]
set d [spixfer5 0x05 0xdb 0 0 0]
echo [format "GFLTEV=%02x%02x" [lindex $d 2] [lindex $d 3]]
set d [spixfer5 0x67 0xf2 0 0 0]
echo [format "P2CFG0=%02x%02x" [lindex $d 2] [lindex $d 3]]
spixfer5 0x66 0xf5 0x00 0x00 0x4e
echo "puerto deshabilitado (cleanup)"
resume
shutdown