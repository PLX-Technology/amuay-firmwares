# Deja el puerto 1 del MPS RETENIDO en clasificacion para medir con el
# multimetro en el conector del slot.
#
# Por que hace falta: la ventana de clasificacion del firmware son 4 ms cada
# ~5 s, imposible de medir a mano. Aqui se entra en clasificacion y se deja
# el nucleo HALTADO 60 s, asi el firmware no interviene y el LTC4296 mantiene
# su configuracion. Al terminar deshabilita el puerto y reanuda.
#
# QUE MEDIR, en el conector del slot 2 (donde esta el modulo), entre PWR_P y
# PWR_N, mientras el script espera:
#
#   ~5 V   -> la tension de clasificacion SI llega al conector. Entonces el
#             fallo esta en el camino de sensado del modulo hacia sccpi, o en
#             la pista sccpi del MPS.
#   ~0 V   -> no llega. El fallo esta entre la salida del LTC4296 y el
#             conector del slot (pista, conector, o el retorno de lado bajo
#             compartido Q18).
#
# Referencia: en el MFS, un puerto con modulo en clasificacion levanta sccpi
# a 1; en el MPS se queda en 0 con el mismo modulo.
#
# SEGURIDAD: prebias + clasificacion, corriente limitada (uA-mA). NO se pone
# SW_POWER_AVAILABLE ni se llama a force_port_pwr, asi que NO hay entrega de
# potencia. Es lo mismo que el firmware hace solo cada ~5 s, solo que
# sostenido. El puerto queda DESHABILITADO al salir.

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

init
halt
echo [format "GPIO2 OUTEN = 0x%08x  (control: la app tiene que estar corriendo)" \
      [lindex [read_memory 0x4000A00C 32 1] 0]]

spixfer5 0x48 0x3f 0x01 0x08 0x63     ;# P1CFG1 = 0x0108
spixfer5 0x46 0x15 0x20 0x41 0x20     ;# P1CFG0 = 0x2041
sleep 50
set st [rd16 {0x45 0x1c 0 0 0}]
echo [format "P1ST = 0x%04x  estado=%d  (3 = SEARCHING)   sccpi1 = %d" \
      $st [expr {$st & 0x7}] [expr {([lindex [read_memory 0x4000A024 32 1] 0] >> 16) & 1}]]
echo ""
echo ">>> MIDE AHORA en el conector del slot 2, entre PWR_P y PWR_N."
echo ">>> ~5 V = la clasificacion llega  |  ~0 V = no llega."
echo ">>> Tienes 60 segundos."
sleep 60000

set st [rd16 {0x45 0x1c 0 0 0}]
echo [format "al terminar: P1ST = 0x%04x  estado=%d  sccpi1 = %d" \
      $st [expr {$st & 0x7}] [expr {([lindex [read_memory 0x4000A024 32 1] 0] >> 16) & 1}]]
spixfer5 0x46 0x15 0x00 0x00 0x4e     ;# deshabilitar
echo "puerto 1 deshabilitado"
resume
shutdown
