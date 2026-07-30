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

# ⚠️ Si la placa esta en la ROM, el reloj de SPI0 esta apagado: las
# escrituras al LTC4296 se pierden y todo lee 0x0000. Abortar, o se mide
# una ventana que nunca existio.
set outen [lindex [read_memory 0x4000A00C 32 1] 0]
echo [format "GPIO2 OUTEN = 0x%08x" $outen]
if {$outen == 0} {
  echo ""
  echo "ABORTADO: la placa esta en la ROM, no en la aplicacion."
  echo "  -> quitar la cinta SWD, POR de 10 s, y reconectar ya arrancada."
  resume
  shutdown
}

spixfer5 0x48 0x3f 0x01 0x08 0x63     ;# P1CFG1 = 0x0108
spixfer5 0x46 0x15 0x20 0x41 0x20     ;# P1CFG0 = 0x2041
sleep 50
echo ""
echo ">>> MIDE AHORA en el conector del slot 2, entre PWR_P y PWR_N."
echo ">>> ~5 V = la clasificacion llega  |  ~0 V = no llega.   (60 s)"
echo ""

# Espera troceada: un `sleep 60000` de golpe tumba el enlace CMSIS-DAP
# (rafaga de errores hid_write). Leyendo cada 2 s se mantiene vivo, y de
# paso se confirma que el puerto AGUANTA en SEARCHING toda la ventana.
for {set i 0} {$i < 30} {incr i} {
  sleep 2000
  set st [rd16 {0x45 0x1c 0 0 0}]
  echo [format "  %2ds  P1ST=0x%04x  estado=%d %s  sccpi1=%d" \
        [expr {($i + 1) * 2}] $st [expr {$st & 0x7}] \
        [expr {($st & 0x7) == 3 ? "SEARCHING" : "         "}] \
        [expr {([lindex [read_memory 0x4000A024 32 1] 0] >> 16) & 1}]]
}

spixfer5 0x46 0x15 0x00 0x00 0x4e     ;# deshabilitar
echo "puerto 1 deshabilitado"
resume
shutdown
