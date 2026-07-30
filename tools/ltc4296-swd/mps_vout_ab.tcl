# A/B de la tension de SONDEO en el MPS: puerto 0 (PSM + PDM del MFS)
# contra puerto 1 (slot vacio, control).
#
# Por que: con el PSM puesto, la negociacion da rc=1 (PD_LINE_NOT_HIGH) y
# PxST=0x3000 (DISABLED) -- exactamente lo mismo que da un slot VACIO. Los
# registros no distinguen "no hay PD" de "el par esta en corto". El sondeo si:
#     ~5145 mV = puerto sano contra abierto
#      35-70 mV = corto DC en el par (medido en el FSW, jul-2026)
#
# SEGURIDAD: solo se escribe PxCFG1=0x0108 (prebias) y PxCFG0=0x2041
# (SET_CLASSIFICATION_MODE | SW_PSE_READY | SW_EN). El bit
# SW_POWER_AVAILABLE (BIT5) NO se pone y no se llama a force_port_pwr:
# es modo clasificacion, corriente limitada. Es byte a byte la misma
# secuencia que el firmware ya ejecuta solo cada ~5 s en retry_spoe_sccp.
# Cada puerto queda DESHABILITADO (PxCFG0=0x0000) al salir.
#
# Frames de 5 bytes generados con el PEC del driver (CRC8 seed 0x41),
# validados reproduciendo los de vout_p3.tcl / gadc_vout.tcl.

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

# GADCDAT -> mV.  bit12 = NEW (dato fresco), bits 11:0 = codigo
proc vout {} {
  set raw [rd16 {0x17 0xa5 0 0 0}]
  set mv  [expr {(($raw & 0xFFF) - 2048) * 35}]
  return [list $mv $raw]
}

# sccpi: P2.14 (puerto 0) y P2.16 (puerto 1), GPIO2 IN = 0x4000A024
proc sccpi {bit} {
  return [expr {([lindex [read_memory 0x4000A024 32 1] 0] >> $bit) & 1}]
}

init
halt

echo "===== globales ====="
echo [format "GCFG   = 0x%04x   (0x0009 = vivo con 50 V, protecciones ON)" [rd16 {0x13 0xb9 0 0 0}]]
echo [format "GSTAT  = 0x%04x" [rd16 {0x03 0xc9 0 0 0}]]
echo [format "GFLTEV = 0x%04x   (0 = sin fallos)" [rd16 {0x05 0xdb 0 0 0}]]
echo [format "sccpi reposo: p0(P2.14)=%d  p1(P2.16)=%d" [sccpi 14] [sccpi 16]]

# ---------- PUERTO 0 : slot 1, PSM cableado al PDM del MFS ----------
echo ""
echo "===== PUERTO 0 (slot 1, PSM -> PDM del MFS) ====="
spixfer5 0x28 0x18 0x01 0x08 0x63    ;# P0CFG1 = 0x0108  prebias SCCP
spixfer5 0x26 0x32 0x20 0x41 0x20    ;# P0CFG0 = 0x2041  en + clasificacion
sleep 30
spixfer5 0x14 0xac 0x00 0x44 0x95    ;# GADCCFG = vout puerto 0
sleep 10
for {set i 0} {$i < 12} {incr i} {
  set v [vout]
  echo [format "  t%02d Vout0 = %6d mV  (GADCDAT=0x%04x)  sccpi0=%d" \
        $i [lindex $v 0] [lindex $v 1] [sccpi 14]]
  sleep 5
}
echo [format "  P0ST = 0x%04x    P0EV = 0x%04x" [rd16 {0x25 0x3b 0 0 0}] [rd16 {0x21 0x27 0 0 0}]]
spixfer5 0x26 0x32 0x00 0x00 0x4e    ;# P0CFG0 = 0  DESHABILITAR
sleep 20

# ---------- PUERTO 1 : slot 2 vacio, control ----------
echo ""
echo "===== PUERTO 1 (slot 2 VACIO, control) ====="
spixfer5 0x48 0x3f 0x01 0x08 0x63    ;# P1CFG1 = 0x0108
spixfer5 0x46 0x15 0x20 0x41 0x20    ;# P1CFG0 = 0x2041
sleep 30
spixfer5 0x14 0xac 0x00 0x46 0x9b    ;# GADCCFG = vout puerto 1
sleep 10
for {set i 0} {$i < 12} {incr i} {
  set v [vout]
  echo [format "  t%02d Vout1 = %6d mV  (GADCDAT=0x%04x)  sccpi1=%d" \
        $i [lindex $v 0] [lindex $v 1] [sccpi 16]]
  sleep 5
}
echo [format "  P1ST = 0x%04x    P1EV = 0x%04x" [rd16 {0x45 0x1c 0 0 0}] [rd16 {0x41 0x00 0 0 0}]]
spixfer5 0x46 0x15 0x00 0x00 0x4e    ;# P1CFG0 = 0  DESHABILITAR

spixfer5 0x14 0xac 0x00 0x00 0x4e    ;# GADC apagado
echo ""
echo "ambos puertos deshabilitados, GADC apagado"
resume
shutdown
