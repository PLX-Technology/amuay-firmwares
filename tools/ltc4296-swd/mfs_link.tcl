# Estado de enlace de los 6 puertos del MFS.
#
# Direccion sacada del .map del build que REALMENTE corre: build_class13nr,
# identificado porque su zephyr.bin tiene el mismo SHA que el payload de
# mfs_clean_class13.sbin y su __start (0x10005fd4) coincide con el jump de la
# cabecera del .sbin.
#
#   g_link[6] = 0x20097914
#
# ⚠️ Ese build NO tiene g_phyid1 (esa global solo existia en mfs_fix), asi que
# aqui no se puede distinguir "PHY no identificado" de "PHY sin portadora".
#
# ⚠️ Y ojo: SES_GetLinkState ha dado links fantasma antes (paso con el RJ45
# del MPS). Un 1 hay que confirmarlo con trafico real; un 0 si es fiable.
#
# Mapeo de slots del MFS: el slot rotulado Port 1 es el uplink PDM; los slots
# PSE Port 2..6 son los puertos LTC 0..4. Para el plano de DATOS los 6 puertos
# SES corresponden a los 6 slots.
#
# Solo LEE.

init
halt

set outen [lindex [read_memory 0x4000A00C 32 1] 0]
echo [format "GPIO2 OUTEN = 0x%08x  (0 = esta en la ROM, la lectura NO vale)" $outen]
if {$outen == 0} {
  echo "ABORTADO: cinta SWD fuera, POR de 10 s, esperar al arranque seguro, reconectar."
  resume
  shutdown
}

echo ""
echo "g_link en 0x20097914 (6 puertos):"
set v [read_memory 0x20097914 32 6]
set i 0
foreach w $v {
  set marca ""
  if {$w != 0} { set marca "<<< ENLACE" }
  echo [format "  puerto %d (slot Port %d)  link = %d  %s" $i [expr {$i + 1}] $w $marca]
  incr i
}

# muestreo, por si el enlace sube y baja
echo ""
echo "muestreo (10 x 1 s):"
for {set i 0} {$i < 10} {incr i} {
  set v [read_memory 0x20097914 32 6]
  echo [format "  t%02d  %d %d %d %d %d %d" $i \
        [lindex $v 0] [lindex $v 1] [lindex $v 2] \
        [lindex $v 3] [lindex $v 4] [lindex $v 5]]
  sleep 1000
}
resume
shutdown
