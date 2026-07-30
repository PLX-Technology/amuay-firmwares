# Verificacion del slot 4 del MPS despues del rework de polaridad.
#
# Slot 4 = puerto 3 del LTC4296 (el mapeo slot N <-> puerto N-1 esta
# comprobado empiricamente: modulo en slot 1 -> sccpi0, en slot 2 -> sccpi1).
# sccpi3 = P2.21.
#
# Comprueba en orden. Si un paso falla, los siguientes no significan nada:
#
#   1. Vin        >= 50000 mV   (con el riel a 54 V debe rondar 52500)
#   2. sccpi3     0 -> 1 al entrar en SEARCHING   <-- LA PRUEBA DEL REWORK
#   3. PxST       estado 2 (DELIVERING) y POWERED=1 con el firmware corriendo
#
# Ademas deja el puerto retenido en clasificacion 120 s para medir con el
# multimetro en el zocalo: debe dar +5 V (antes del rework daba -5 V).
#
# SEGURIDAD: prebias + clasificacion, corriente limitada. Nunca
# SW_POWER_AVAILABLE, nunca force_port_pwr. Deshabilita el puerto al salir.

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
proc sccpi3 {} { return [expr {([lindex [read_memory 0x4000A024 32 1] 0] >> 21) & 1}] }

init
halt

set outen [lindex [read_memory 0x4000A00C 32 1] 0]
echo [format "GPIO2 OUTEN = 0x%08x" $outen]
if {$outen == 0} {
  echo "ABORTADO: la placa esta en la ROM. Cinta SWD fuera, POR de 10 s,"
  echo "esperar a que termine el arranque seguro, y reconectar."
  resume
  shutdown
}

echo ""
echo "=== 1. estado global ==="
echo [format "GCFG = 0x%04x   GFLTEV = 0x%04x  (0x0009 / 0x0000 = sano)" \
      [rd16 {0x13 0xb9 0 0 0}] [rd16 {0x05 0xdb 0 0 0}]]
spixfer5 0x14 0xac 0x00 0x41 0x8e
sleep 30
set raw [rd16 {0x17 0xa5 0 0 0}]
set vin [expr {(($raw & 0xFFF) - 2048) * 35}]
echo [format "Vin = %d mV   %s" $vin \
      [expr {$vin >= 50000 ? "pasa el piso de Clase 13" : "NO LLEGA AL PISO -> subir el riel a 54 V"}]]
spixfer5 0x14 0xac 0x00 0x00 0x4e

echo ""
echo "=== 2. lo que ya negocia el firmware por su cuenta ==="
foreach p {0 1 2 3} f {{0x25 0x3b 0 0 0} {0x45 0x1c 0 0 0} {0x65 0xfc 0 0 0} {0x85 0x52 0 0 0}} {
  set st [rd16 $f]
  echo [format "  P%dST = 0x%04x  estado=%d  POWERED=%d  %s" $p $st \
        [expr {$st & 0x7}] [expr {($st >> 4) & 1}] \
        [expr {($st & 0x7) == 2 ? "<<< ENTREGANDO" : ""}]]
}
echo [format "  g_retry_rc = %s" [format "%d,%d,%d,%d" \
      {*}[read_memory 0x20097900 32 4]]]
echo [format "  g_lg_any   = %d" [lindex [read_memory 0x20097914 32 1] 0]]

echo ""
echo "=== 3. slot 4 (puerto 3) retenido en clasificacion ==="

# ⚠️ Si el puerto YA esta entregando, no tocarlo: forzar clasificacion sobre
# una entrega viva la tumba. (Paso en la primera pasada; el bucle de
# reintento la recupero en ~5 s, pero no hay que provocarlo.)
set st [rd16 {0x85 0x52 0 0 0}]
if {($st & 0x7) == 2} {
  echo ""
  echo "  El puerto 3 YA ESTA ENTREGANDO (PxST=0x[format %04x $st]). No se toca."
  echo "  El rework funciono: no hace falta la retencion."
  resume
  shutdown
}

echo ">>> MIDE en el zocalo del slot 4: debe dar +5 V (antes -5 V).  (120 s)"
echo ">>> Y mira la columna sccpi3: 1 = el rework FUNCIONO."
echo ""
spixfer5 0x88 0x71 0x01 0x08 0x63     ;# P3CFG1 = 0x0108
spixfer5 0x86 0x5b 0x20 0x41 0x20     ;# P3CFG0 = 0x2041
for {set i 0} {$i < 60} {incr i} {
  sleep 2000
  set st [rd16 {0x85 0x52 0 0 0}]
  echo [format "  %3ds  P3ST=0x%04x  estado=%d %s  sccpi3=%d %s" \
        [expr {($i + 1) * 2}] $st [expr {$st & 0x7}] \
        [expr {($st & 0x7) == 3 ? "SEARCHING" : "         "}] \
        [sccpi3] [expr {[sccpi3] ? "<<< LINEA ALTA" : ""}]]
}
spixfer5 0x86 0x5b 0x00 0x00 0x4e     ;# deshabilitar
echo "puerto 3 deshabilitado"
resume
shutdown
