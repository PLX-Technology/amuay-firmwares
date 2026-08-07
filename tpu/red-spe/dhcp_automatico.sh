#!/bin/bash
# Deja que una ATT NUEVA reciba IP sola, sin declararla antes a mano.
#
#     sudo bash dhcp_automatico.sh
#
# EL PROBLEMA: hoy el dnsmasq de pcie0 es una lista blanca pura --
# `dhcp-range=...,static` + `dhcp-ignore=tag:!known`. Solo las MAC ya escritas
# en el fichero reciben direccion. Para 3 placas se aguanta; para 50 significa
# editar la configuracion y reiniciar el DHCP por cada placa que se instala, y
# ademas hay que averiguar su MAC leyendo el log -- que es justo lo que se
# quiere evitar en la refineria.
#
# LA SOLUCION, sin abrir el segmento a cualquiera: dnsmasq puede etiquetar por
# PATRON DE MAC. Las ATT usan todas el prefijo 02:00:70. Se les da un rango
# dinamico propio; a cualquier otro equipo se le sigue ignorando.
#
#   dhcp-mac=set:att,02:00:70:*:*:*        <- etiqueta a las ATT por prefijo
#   dhcp-range=tag:att,192.168.50.200,192.168.50.250,255.255.255.0,1h
#   dhcp-ignore=tag:!known,tag:!att        <- ignora solo lo que NO es ninguna
#
# Las etiquetas de un dhcp-ignore se combinan con Y: se ignora al que no esta
# declarado Y ademas no parece una ATT. Las reservas fijas siguen mandando para
# las placas ya declaradas, asi que nada de lo que funciona cambia.
#
# Concesion de 1 h a proposito: una placa de banco que se conecta y se lleva no
# deja su direccion ocupada durante medio dia.
#
# ⚠️ Los respaldos van FUERA de /etc/dnsmasq.d: dnsmasq lee TODOS los ficheros
# de ese directorio y solo excluye .dpkg-dist/.dpkg-old/.dpkg-new. Un .bak ahi
# dentro se carga como una segunda configuracion y el servicio no arranca
# ("illegal repeated keyword"). Paso el 2026-08-07.
set -u

DIR=/etc/dnsmasq.d
CONF=$DIR/varec-spe.conf
BAKDIR=/var/backups/varec-spe
PREFIJO='02:00:70'
POOL_INI=192.168.50.200
POOL_FIN=192.168.50.250

[ -f "$CONF" ] || { echo "!! no encuentro $CONF"; exit 1; }
mkdir -p "$BAKDIR"

# --- 0) barrer respaldos sueltos que impidan arrancar --------------------
sueltos=$(ls "$DIR" 2>/dev/null | grep -E '\.bak|~$|\.orig$|\.save$' || true)
if [ -n "$sueltos" ]; then
    echo "== respaldos sueltos en $DIR (dnsmasq los lee): los aparto"
    for f in $sueltos; do mv -v "$DIR/$f" "$BAKDIR/" | sed 's/^/    /'; done
fi

if grep -q '^dhcp-mac=set:att' "$CONF"; then
    echo "== ya esta configurado, no se toca"
else
    RESP="$BAKDIR/varec-spe.conf.$(date +%Y%m%d-%H%M%S)"
    cp -a "$CONF" "$RESP"; echo "== respaldo: $RESP"

    # El dhcp-ignore viejo dejaria fuera a las ATT nuevas: hay que ampliarlo.
    sed -i 's|^dhcp-ignore=tag:!known$|dhcp-ignore=tag:!known,tag:!att|' "$CONF"

    cat >> "$CONF" <<EOF

# --- alta automatica de ATT nuevas (ver dhcp_automatico.sh) ---
# Las ATT llevan el prefijo $PREFIJO. Se las etiqueta por patron y se les da un
# rango propio, para que una placa recien instalada tome IP sin declararla
# antes. Cualquier otro equipo sigue sin recibir direccion.
dhcp-mac=set:att,$PREFIJO:*:*:*
dhcp-range=tag:att,$POOL_INI,$POOL_FIN,255.255.255.0,1h
EOF
    echo "== anadido:"; tail -5 "$CONF" | sed 's/^/    /'
fi

# --- validar ANTES de reiniciar -----------------------------------------
# El estado se captura en una variable. Encadenar `| sed` aqui hace que el `if`
# lea el estado de salida de sed -- que siempre va bien -- y deje pasar una
# configuracion rota. Ese fue el fallo del 2026-08-07.
salida=$(dnsmasq --test -C /dev/null -7 "$DIR" 2>&1); estado=$?
echo "$salida" | sed 's/^/    /'
if [ $estado -ne 0 ]; then
    echo "!! configuracion invalida (dnsmasq --test salio $estado). No reinicio."
    [ -n "${RESP:-}" ] && { cp -a "$RESP" "$CONF"; echo "!! restaurado $RESP"; }
    exit 1
fi

systemctl reset-failed dnsmasq 2>/dev/null
systemctl restart dnsmasq
sleep 2
if ! systemctl is-active --quiet dnsmasq; then
    echo "!! dnsmasq NO arranco:"
    systemctl status dnsmasq --no-pager -n 10 | sed 's/^/    /'
    [ -n "${RESP:-}" ] && { cp -a "$RESP" "$CONF"; systemctl reset-failed dnsmasq
                            systemctl restart dnsmasq; echo "!! restaurado y reiniciado"; }
    exit 1
fi
echo "== dnsmasq activo"
echo
echo "== configuracion vigente:"
grep -E '^dhcp-(range|ignore|mac|host)' "$CONF" | sed 's/^/    /'
echo
echo "== esperando a que alguna placa nueva pida direccion (hasta 90 s)..."
antes=$(wc -l < /var/lib/misc/dnsmasq.leases 2>/dev/null || echo 0)
for i in $(seq 1 18); do
    ahora=$(wc -l < /var/lib/misc/dnsmasq.leases 2>/dev/null || echo 0)
    if [ "$ahora" -gt "$antes" ]; then
        echo "   concesiones ahora:"; cat /var/lib/misc/dnsmasq.leases | sed 's/^/    /'
        exit 0
    fi
    sleep 5
done
echo "   sin concesiones nuevas todavia. Las ATT reintentan cada ~64 s:"
echo "     journalctl -u dnsmasq --since '-5 min' | grep -i dhcp"
