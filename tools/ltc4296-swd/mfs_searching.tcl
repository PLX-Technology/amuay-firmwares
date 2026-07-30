# REFERENCIA: ¿cuanto vale sccpi en el MFS DURANTE SEARCHING?
#
# Es la medida que falta para poder juzgar el MPS. En el MPS, con un modulo
# BUENO puesto, Vin valido y el puerto en SEARCHING, sccpi se queda en 0 y
# sccp_reset_pulse aborta con PD_LINE_NOT_HIGH. No se puede saber si eso es
# anormal sin ver el valor en la placa que negocia.
#
# ⚠️ La medida anterior del MFS (sccpi=0 con modulo) se tomo con los puertos
# DISABLED y NO sirve como referencia: el instante que importa es SEARCHING.
#
# Barre los 5 puertos. Para cada uno:
#   - si ya esta DELIVERING (estado 2) lo SALTA, para no tumbar una entrega
#     que este funcionando
#   - si no, prebias + clasificacion, lee PxST y sccpi, y lo deshabilita
#
# SEGURIDAD: prebias + clasificacion (corriente limitada), lo mismo que el
# firmware hace solo. Nunca SW_POWER_AVAILABLE. Deshabilita al salir.

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
proc nombre {e} {
  switch $e {
    0 {return "DISABLED"} 1 {return "SLEEPING"} 2 {return "DELIVERING"}
    3 {return "SEARCHING"} 4 {return "ERROR"} 5 {return "IDLE"}
    6 {return "PREPDET"} default {return "UNKNOWN"}
  }
}

# p, bit de sccpi, y los cuatro frames de ese puerto
proc prueba {p ibit fcfg1 fcfg0 fcfg0off fst} {
  set st [rd16 $fst]
  set e [expr {$st & 0x7}]
  echo ""
  echo [format "--- puerto %d (slot Port %d) --- de partida PxST=0x%04x %s, sccpi=%d" \
        $p [expr {$p + 2}] $st [nombre $e] [sccpi $ibit]]
  if {$e == 2} {
    echo "    ENTREGANDO: no se toca."
    return
  }
  eval spixfer5 $fcfg1
  eval spixfer5 $fcfg0
  for {set i 0} {$i < 5} {incr i} {
    set st [rd16 $fst]
    echo [format "    t%d  PxST=0x%04x  estado=%d %-10s  sccpi=%d" \
          $i $st [expr {$st & 0x7}] [nombre [expr {$st & 0x7}]] [sccpi $ibit]]
  }
  eval spixfer5 $fcfg0off
  sleep 20
}

init
halt
echo [format "GPIO2 OUTEN = 0x%08x  (control: 0 = esta en la ROM, no vale nada)" \
      [lindex [read_memory 0x4000A00C 32 1] 0]]
echo [format "GCFG = 0x%04x   GFLTEV = 0x%04x" [rd16 {0x13 0xb9 0 0 0}] [rd16 {0x05 0xdb 0 0 0}]]

prueba 0 14 {0x28 0x18 0x01 0x08 0x63} {0x26 0x32 0x20 0x41 0x20} {0x26 0x32 0x00 0x00 0x4e} {0x25 0x3b 0 0 0}
prueba 1 16 {0x48 0x3f 0x01 0x08 0x63} {0x46 0x15 0x20 0x41 0x20} {0x46 0x15 0x00 0x00 0x4e} {0x45 0x1c 0 0 0}
prueba 2 18 {0x68 0xdf 0x01 0x08 0x63} {0x66 0xf5 0x20 0x41 0x20} {0x66 0xf5 0x00 0x00 0x4e} {0x65 0xfc 0 0 0}
prueba 3 21 {0x88 0x71 0x01 0x08 0x63} {0x86 0x5b 0x20 0x41 0x20} {0x86 0x5b 0x00 0x00 0x4e} {0x85 0x52 0 0 0}
prueba 4 23 {0xa8 0x91 0x01 0x08 0x63} {0xa6 0xbb 0x20 0x41 0x20} {0xa6 0xbb 0x00 0x00 0x4e} {0xa5 0xb2 0 0 0}

echo ""
echo "todos los puertos tocados quedan deshabilitados"
resume
shutdown
