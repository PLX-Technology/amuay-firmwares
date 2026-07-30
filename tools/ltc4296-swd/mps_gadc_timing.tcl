# ¿Le da tiempo al GADC a tener el bit NEW en la ventana que usa el firmware?
#
# do_spoe_sccp hace, antes de clasificar:
#     disable_gadc            (GADCCFG = 0x0000)
#     k_sleep(4 ms)
#     set_gadc_vin            (GADCCFG = 0x0041, + 4 ms dentro de la funcion)
#     k_sleep(4 ms)           => 8 ms desde la escritura
#     read_gadc  -> EXIGE el bit NEW (bit12); si no esta, devuelve
#                   INVALID_ADC_VOLTAGE y do_spoe_sccp sale con
#                   DISCONTINUE_SCCP sin tocar el puerto
#
# Sintomas que explicaria: retry_rc = 1 en LOS CUATRO puertos (el ADC es
# global), y el LED del PSM sin parpadear (nunca se entra en clasificacion).
#
# Aqui se replica esa secuencia exacta y se barre el tiempo de espera para
# ver cuando aparece NEW.
#
# Solo se escribe GADCCFG (selector del ADC). Ningun PxCFG, ningun puerto.

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

# Una pasada completa igual que el firmware, esperando $espera ms tras
# escribir GADCCFG. Devuelve 1 si NEW estaba puesto.
proc pasada {espera} {
  spixfer5 0x14 0xac 0x00 0x00 0x4e     ;# GADCCFG = 0      disable_gadc
  sleep 4
  spixfer5 0x14 0xac 0x00 0x41 0x8e     ;# GADCCFG = 0x0041 set_gadc_vin
  sleep $espera
  set raw [rd16 {0x17 0xa5 0 0 0}]
  set new [expr {($raw >> 12) & 1}]
  echo [format "  espera %3d ms  GADCDAT=0x%04x  NEW=%d MISSED=%d  Vin=%6d mV   %s" \
        $espera $raw $new [expr {($raw >> 13) & 1}] \
        [expr {(($raw & 0xFFF) - 2048) * 35}] \
        [expr {$new ? "read_gadc OK" : "read_gadc FALLA -> DISCONTINUE_SCCP"}]]
  return $new
}

init
halt
echo [format "GPIO2 OUTEN = 0x%08x  (control: la app corriendo)" \
      [lindex [read_memory 0x4000A00C 32 1] 0]]
echo ""
echo "La ventana del firmware son 8 ms. Barrido:"
foreach t {8 8 8 4 16 32 64 128} { pasada $t }
echo ""
echo "repeticion de la ventana real (8 ms) x6, por si es intermitente:"
set ok 0
for {set i 0} {$i < 6} {incr i} { set ok [expr {$ok + [pasada 8]}] }
echo [format "  -> NEW presente en %d de 6 intentos a 8 ms" $ok]

spixfer5 0x14 0xac 0x00 0x00 0x4e
resume
shutdown
