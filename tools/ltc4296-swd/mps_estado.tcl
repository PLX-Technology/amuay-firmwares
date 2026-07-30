# Estado completo de negociacion del MPS: registros del chip + globales del
# firmware. Solo lectura.
#
# Que buscar:
#   PxST bits 2:0 = 2  y POWERED = 1   -> ese puerto ESTA ENTREGANDO
#   g_retry_rc distinto de 1           -> ya no sale por DISCONTINUE_SCCP
#   g_lg_any = 1                       -> el firmware ve entrega
#
# Direcciones de las globales: build de produccion pse_safe_class13
# (jump 0x1000650c). Si se recompila, hay que releerlas del .map.

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
proc st {p n} {
  echo [format "  P%dST = 0x%04x  estado=%d %s  POWERED=%d PREBIASED=%d DETECTING=%d  DET_VLOW=%d DET_VHIGH=%d" \
        $p $n [expr {$n & 0x7}] [expr {($n & 0x7) == 2 ? "<<< ENTREGANDO" : "             "}] \
        [expr {($n >> 4) & 1}] [expr {($n >> 6) & 1}] [expr {($n >> 5) & 1}] \
        [expr {($n >> 12) & 1}] [expr {($n >> 13) & 1}]]
}
proc w32 {a} { return [lindex [read_memory $a 32 1] 0] }
proc arr4 {a} {
  set v [read_memory $a 32 4]
  return [format "%d,%d,%d,%d" [lindex $v 0] [lindex $v 1] [lindex $v 2] [lindex $v 3]]
}

init
halt
echo [format "GPIO2 OUTEN = 0x%08x  (control: la app corriendo)" [w32 0x4000A00C]]
echo ""
echo "=== LTC4296 ==="
echo [format "GCFG = 0x%04x   GFLTEV = 0x%04x" [rd16 {0x13 0xb9 0 0 0}] [rd16 {0x05 0xdb 0 0 0}]]
spixfer5 0x14 0xac 0x00 0x41 0x8e
sleep 30
set raw [rd16 {0x17 0xa5 0 0 0}]
echo [format "Vin  = %d mV   (piso Clase 13 = 50000, techo = 58000)" \
      [expr {(($raw & 0xFFF) - 2048) * 35}]]
spixfer5 0x14 0xac 0x00 0x00 0x4e
echo ""
st 0 [rd16 {0x25 0x3b 0 0 0}]
st 1 [rd16 {0x45 0x1c 0 0 0}]
st 2 [rd16 {0x65 0xfc 0 0 0}]
st 3 [rd16 {0x85 0x52 0 0 0}]
echo ""
echo "=== globales del firmware ==="
echo [format "g_retry_rc   = %s   (1 = DISCONTINUE_SCCP, 2 = SCCP_COMPLETE, 4 = PD_NOT_PRESENT)" [arr4 0x20097900]]
echo [format "g_lg_deliver = %s" [arr4 0x20097918]]
echo [format "g_lg_st      = %s" [arr4 0x20099174]]
echo [format "g_lg_any     = %d   <- 1 = el firmware ve entrega" [w32 0x20097914]]
echo [format "g_lg_gfltev  = 0x%04x   g_rd_err_n = %d" [w32 0x20099184] [w32 0x20097998]]
echo [format "g_link       = %s" [arr4 0x20097980]]
echo ""
echo "sccpi: p0=%d p1=%d p2=%d p3=%d"
echo [format "  %d %d %d %d" [expr {([w32 0x4000A024] >> 14) & 1}] [expr {([w32 0x4000A024] >> 16) & 1}] \
      [expr {([w32 0x4000A024] >> 18) & 1}] [expr {([w32 0x4000A024] >> 21) & 1}]]
resume
shutdown
