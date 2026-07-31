# Registros del PHY ADIN1100 de los SEIS puertos del MFS.
#
# Direcciones del build de diagnostico build_phy6 (jump 0x100071fc).
# Si se reflashea otra cosa, revalidar con el .map.
#
# Que buscar, por puerto:
#   PHYID1 = 0x0283  -> el SES VE el ADIN1100
#   PHYID1 = 0x0000  -> NO lo identifica  => es phyPullupCtrl (sigue en 0 en
#                       la rama mfs; con 0 el SES no llega a identificar el PHY,
#                       no lo configura, y la autoneg queda apagada)
#   AN control bit12 -> autonegociacion encendida. ESTE es el registro que
#                       resolvio el slot 4 en julio: decia literalmente
#                       "autoneg apagada" mientras se media el lado digital.
#   AN status  bit2  -> link status;  bit5 -> AN completada
#   B10L link status -> portadora real a nivel 10BASE-T1L
#   PMA control      -> maestro/esclavo, por si hay conflicto de roles
#
# El mapeo macPortN <-> slot serigrafiado NO esta documentado (el que si lo
# esta, slot = LTC_port + 2, es el de POTENCIA). Se identifica por quien
# responda con un PHY vivo.
#
# Solo LEE.

proc arr16 {addr n} { return [read_memory $addr 16 $n] }

init
halt

set outen [lindex [read_memory 0x4000A00C 32 1] 0]
echo [format "GPIO2 OUTEN = 0x%08x" $outen]
if {$outen == 0} {
  echo "ABORTADO: la placa esta en la ROM. Cinta fuera, POR de 10 s, esperar, reconectar."
  resume
  shutdown
}

set link  [read_memory 0x20097984 32 6]
set rc    [read_memory 0x20097924 32 6]
set id1   [arr16 0x200991ea 6]
set id2   [arr16 0x200991de 6]
set b10l  [arr16 0x200991d2 6]
set anst  [arr16 0x200991c6 6]
set anctl [arr16 0x200991ba 6]
set pma   [arr16 0x200991ae 6]

echo ""
echo "puerto  link  rc   PHYID1  PHYID2  B10L    ANstat  ANctl   PMA     autoneg"
for {set i 0} {$i < 6} {incr i} {
  set a [lindex $anctl $i]
  set an "OFF"
  if {[expr {$a & 0x1000}]} { set an "ON" }
  set visto ""
  if {[lindex $id1 $i] == 0x0283} { set visto "  <- ADIN1100 VISTO" }
  echo [format "  %d     %d   %3d  0x%04x  0x%04x  0x%04x  0x%04x  0x%04x  0x%04x  %-3s%s" \
        $i [lindex $link $i] [lindex $rc $i] \
        [lindex $id1 $i] [lindex $id2 $i] [lindex $b10l $i] \
        [lindex $anst $i] [lindex $anctl $i] [lindex $pma $i] $an $visto]
}

echo ""
echo "muestreo de link + B10L (6 x 1 s):"
for {set k 0} {$k < 6} {incr k} {
  set link [read_memory 0x20097984 32 6]
  set b10l [arr16 0x200991d2 6]
  echo [format "  t%d  link %d%d%d%d%d%d   B10L %04x %04x %04x %04x %04x %04x" $k \
        [lindex $link 0] [lindex $link 1] [lindex $link 2] \
        [lindex $link 3] [lindex $link 4] [lindex $link 5] \
        [lindex $b10l 0] [lindex $b10l 1] [lindex $b10l 2] \
        [lindex $b10l 3] [lindex $b10l 4] [lindex $b10l 5]]
  sleep 1000
}
resume
shutdown
