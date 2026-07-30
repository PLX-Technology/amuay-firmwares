# Control: ¿la linea SCCP del slot 1 la baja el PSM, o la bajamos nosotros?
#
# sccpo es ACTIVE_LOW y controla el gate de un FET de pull-down. Si el MCU
# esta conduciendo ese FET, sccpi leeria 0 por nuestra culpa y la lectura
# anterior (p0=0 / p1=1) no probaria nada del modulo.
#
# GPIO2 @ 0x4000A000: OUTEN=+0x0C  OUT=+0x18  IN=+0x24
# puerto 0: sccpi=P2.14 sccpo=P2.15   |   puerto 1: sccpi=P2.16 sccpo=P2.17
# Solo LEE. No escribe nada.

proc bit {addr b} {
  return [expr {([lindex [read_memory $addr 32 1] 0] >> $b) & 1}]
}

init
halt

echo "         sccpi  sccpo(pin)  outen(sccpo)  out(sccpo)"
echo [format "puerto0    %d        %d             %d             %d" \
      [bit 0x4000A024 14] [bit 0x4000A024 15] [bit 0x4000A00C 15] [bit 0x4000A018 15]]
echo [format "puerto1    %d        %d             %d             %d" \
      [bit 0x4000A024 16] [bit 0x4000A024 17] [bit 0x4000A00C 17] [bit 0x4000A018 17]]
echo [format "puerto2    %d        %d             %d             %d" \
      [bit 0x4000A024 18] [bit 0x4000A024 20] [bit 0x4000A00C 20] [bit 0x4000A018 20]]
echo [format "puerto3    %d        %d             %d             %d" \
      [bit 0x4000A024 21] [bit 0x4000A024 22] [bit 0x4000A00C 22] [bit 0x4000A018 22]]
echo ""
echo [format "GPIO2 IN    = 0x%08x" [lindex [read_memory 0x4000A024 32 1] 0]]
echo [format "GPIO2 OUTEN = 0x%08x" [lindex [read_memory 0x4000A00C 32 1] 0]]
echo [format "GPIO2 OUT   = 0x%08x" [lindex [read_memory 0x4000A018 32 1] 0]]

# muestreo repetido de sccpi0: si el PSM/PDM esta arrancando o hay actividad,
# la linea deberia moverse en algun momento
echo ""
echo "muestreo sccpi (20 x 100 ms):"
for {set i 0} {$i < 20} {incr i} {
  echo [format "  t%02d  p0=%d p1=%d p2=%d p3=%d" $i \
        [bit 0x4000A024 14] [bit 0x4000A024 16] [bit 0x4000A024 18] [bit 0x4000A024 21]]
  sleep 100
}
resume
shutdown
