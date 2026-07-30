# Medir Vin en el MPS. Es la comprobacion que decide.
#
# retry_spoe_sccp valida Vin ANTES de clasificar (ltc4296_is_vin_valid). El
# binario del MPS lleva el piso de Clase 13 en 50000 mV; el del MFS que acaba
# de negociar lo lleva en 49000. En el MFS se midio Vin = 49455 mV.
#
#   Vin < 50000  -> el MPS falla la validacion y NUNCA entra en clasificacion
#                   => el LED del PSM no parpadea. Confirmado.
#   Vin >= 50000 -> la hipotesis se cae y hay que buscar otra cosa.
#
# GADCCFG = 0x0041 (ContModeLowGain | Vin), igual que ltc4296_set_gadc_vin.
# Frame generado con gen_frames.py, NO a mano.
#
# Solo escribe GADCCFG (selector del ADC). No toca ningun PxCFG, no habilita
# ningun puerto. Deja el GADC apagado al salir.

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
echo [format "GCFG = 0x%04x   GFLTEV = 0x%04x" \
      [rd16 {0x13 0xb9 0 0 0}] [rd16 {0x05 0xdb 0 0 0}]]
echo ""

spixfer5 0x14 0xac 0x00 0x41 0x8e    ;# GADCCFG = 0x0041  Vin
sleep 50
echo "Vin (piso de Clase 13 en este binario = 50000 mV):"
for {set i 0} {$i < 10} {incr i} {
  set raw [rd16 {0x17 0xa5 0 0 0}]
  set mv [expr {(($raw & 0xFFF) - 2048) * 35}]
  echo [format "  t%02d  GADCDAT=0x%04x  Vin = %6d mV   %s" $i $raw $mv \
        [expr {$mv >= 50000 ? "pasa" : "NO PASA la validacion"}]]
  sleep 100
}

spixfer5 0x14 0xac 0x00 0x00 0x4e    ;# GADC off
resume
shutdown
