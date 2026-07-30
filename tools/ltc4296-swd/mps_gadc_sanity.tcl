# CONTROL DEL INSTRUMENTO: ¿mide algo el GADC, o 5145 mV es un artefacto?
#
# GADCDAT ha salido 0x3893 identico en todas las muestras de todos los
# puertos, vacios y cargados, en tres pasadas. Eso puede ser (a) la tension
# de clasificacion en circuito abierto, igual en todos por ser todos abiertos
# en DC, o (b) una lectura muerta (el select no llega, o el dato esta rancio;
# ojo que el bit13 MISSED esta puesto en 0x3893).
#
# Discriminador: con el puerto DESHABILITADO no hay fuente de clasificacion,
# asi que Vout DEBE leer ~0 mV. Si sigue dando 5145, la medida no vale y hay
# que descartar todas las conclusiones basadas en ella.
#
# Se prueba tambien Vin (GADCCFG=0x0041, lo que usa el propio driver) como
# segunda referencia: debe dar ~50 V.
#
# SEGURIDAD: prebias + clasificacion unicamente, y deshabilita al salir.

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
proc show {etiqueta} {
  set raw [rd16 {0x17 0xa5 0 0 0}]
  echo [format "  %-34s GADCDAT=0x%04x  NEW=%d MISSED=%d  ->%7d mV" \
        $etiqueta $raw [expr {($raw>>12)&1}] [expr {($raw>>13)&1}] \
        [expr {(($raw & 0xFFF) - 2048) * 35}]]
}
proc sccpi1 {} { return [expr {([lindex [read_memory 0x4000A024 32 1] 0] >> 16) & 1}] }

init
halt

echo "=== 1. puerto 1 DESHABILITADO (debe dar ~0 mV si la medida vale) ==="
spixfer5 0x46 0x15 0x00 0x00 0x4e   ;# P1CFG0 = 0  asegurar deshabilitado
sleep 50
spixfer5 0x14 0xac 0x00 0x46 0x9b   ;# GADCCFG = vout puerto 1
sleep 50
show "vout p1, puerto deshabilitado"
sleep 100
show "vout p1, puerto deshabilitado (2)"

echo ""
echo "=== 2. GADC sobre Vin (0x0041, lo que usa el driver) : debe dar ~50 V ==="
spixfer5 0x14 0xac 0x00 0x41 0x8c
sleep 50
show "vin"
sleep 100
show "vin (2)"

echo ""
echo "=== 3. puerto 1 EN CLASIFICACION con el PSM puesto ==="
spixfer5 0x48 0x3f 0x01 0x08 0x63   ;# P1CFG1 = 0x0108
spixfer5 0x46 0x15 0x20 0x41 0x20   ;# P1CFG0 = 0x2041
sleep 30
spixfer5 0x14 0xac 0x00 0x46 0x9b   ;# GADCCFG = vout puerto 1
sleep 50
show "vout p1, en clasificacion"
echo [format "  sccpi1 = %d" [sccpi1]]
sleep 100
show "vout p1, en clasificacion (2)"
echo [format "  sccpi1 = %d" [sccpi1]]
echo [format "  P1ST = 0x%04x" [rd16 {0x45 0x1c 0 0 0}]]

spixfer5 0x46 0x15 0x00 0x00 0x4e   ;# deshabilitar
spixfer5 0x14 0xac 0x00 0x00 0x4e   ;# GADC off
echo ""
echo "puerto 1 deshabilitado, GADC apagado"
resume
shutdown
