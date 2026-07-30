# ¿Cuanto cae Vout de sondeo cuando el modulo SI ve la tension?
#
# En el MPS, un puerto en clasificacion con modulo mide Vout = 5145 mV,
# exactamente igual que un slot VACIO. Eso es lo que mide un circuito
# ABIERTO. Si en el MFS un puerto que funciona mide MENOS que su slot
# vacio, significa que alli el modulo carga la linea -- y que en el MPS
# no la esta cargando, o sea que la tension de clasificacion no le llega
# al conector del slot.
#
# Compara en la misma pasada:
#   p0 (Port 2) con modulo, que levanta sccpi a 1  -> el bueno
#   p1 (Port 3) vacio                              -> el control
#
# SEGURIDAD: prebias + clasificacion, corriente limitada. Deshabilita al salir.

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
proc sccpi {b} { return [expr {([lindex [read_memory 0x4000A024 32 1] 0] >> $b) & 1}] }

proc mide {etiq ibit fcfg1 fcfg0 fgadc fcfg0off fst} {
  eval spixfer5 $fcfg1
  eval spixfer5 $fcfg0
  sleep 30
  eval spixfer5 $fgadc
  sleep 30
  echo ""
  echo "--- $etiq ---"
  for {set i 0} {$i < 4} {incr i} {
    set raw [rd16 {0x17 0xa5 0 0 0}]
    set st  [rd16 $fst]
    echo [format "   Vout = %6d mV   PxST=0x%04x estado=%d   sccpi=%d" \
          [expr {(($raw & 0xFFF) - 2048) * 35}] $st [expr {$st & 0x7}] [sccpi $ibit]]
  }
  eval spixfer5 $fcfg0off
  sleep 20
}

init
halt
echo [format "GPIO2 OUTEN = 0x%08x   (control)" [lindex [read_memory 0x4000A00C 32 1] 0]]
echo "referencia: en el MPS los dos casos daban 5145 mV, indistinguibles"

mide "p0 (Port 2) CON modulo, el que levanta sccpi" 14 \
     {0x28 0x18 0x01 0x08 0x63} {0x26 0x32 0x20 0x41 0x20} {0x14 0xac 0x00 0x44 0x95} \
     {0x26 0x32 0x00 0x00 0x4e} {0x25 0x3b 0 0 0}

mide "p1 (Port 3) VACIO, control" 16 \
     {0x48 0x3f 0x01 0x08 0x63} {0x46 0x15 0x20 0x41 0x20} {0x14 0xac 0x00 0x46 0x9b} \
     {0x46 0x15 0x00 0x00 0x4e} {0x45 0x1c 0 0 0}

mide "p4 (Port 6) CON modulo, el que NO levanta sccpi" 23 \
     {0xa8 0x91 0x01 0x08 0x63} {0xa6 0xbb 0x20 0x41 0x20} {0x14 0xac 0x00 0x4c 0xad} \
     {0xa6 0xbb 0x00 0x00 0x4e} {0xa5 0xb2 0 0 0}

spixfer5 0x14 0xac 0x00 0x00 0x4e
echo ""
echo "puertos deshabilitados, GADC apagado"
resume
shutdown
