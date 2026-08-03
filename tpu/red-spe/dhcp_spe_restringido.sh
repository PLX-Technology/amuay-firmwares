#!/bin/bash
# DHCP del segmento SPE, SOLO para las ATT nominadas por MAC.
#
# ⚠️ POR QUE RESTRINGIDO Y NO ABIERTO
# La version anterior servia a cualquiera que preguntara, y el segmento NO
# estaba aislado: contesto a maquinas Windows de la oficina (se vio un
# DHCPACK a plvsrvdvr2, 172.16.31.101, por pcie0) y compitio con el DHCP
# corporativo. Con `dhcp-range=...,static` + `dhcp-ignore=tag:!known`,
# dnsmasq IGNORA todo lo que no este en la lista de abajo. Aunque el
# aislamiento del cable no sea perfecto, esto no puede molestar a nadie.
#
# Direcciones fijas por MAC a proposito: para hablar Modbus con una ATT hay
# que saber donde esta, y una direccion del monton obliga a buscarla cada vez.
#
# MACs: se derivan del UID del STM32 y la placa usa la del PUERTO 2 (base+1),
# que es la que transmite. Se nombran las dos por si se cambia el cableado.
#   ATT tank 1  -> 02:00:70:22:30:5a / :5b   (consola: "MAC derivada del UID")
#   ATT tank 21 -> 02:00:70:2d:30:08 / :09   (:09 es la vista en la pasarela)

set -euo pipefail

IFACE=pcie0
CON=varec-spe
IP=192.168.50.1/24
STAMP=$(date +%Y%m%d-%H%M%S)
CFG=/etc/dnsmasq.d/varec-spe.conf
BKP=/var/backups/varec-spe

cd "$(dirname "$(readlink -f "$0")")"
mkdir -p "$BKP"

echo "===== 1) direccion fija en $IFACE ====="
if nmcli -t -f NAME con show | grep -qx "$CON"; then
	echo "  el perfil '$CON' ya existe: se reutiliza"
else
	nmcli connection add type ethernet ifname "$IFACE" con-name "$CON" \
		ipv4.method manual ipv4.addresses "$IP" \
		ipv4.never-default yes ipv6.method disabled \
		connection.autoconnect yes >/dev/null
fi
nmcli connection up "$CON" >/dev/null 2>&1 || true
ip -br addr show "$IFACE"

echo
echo "===== 2) DHCP restringido ====="
# ⚠️ Los respaldos NUNCA dentro de /etc/dnsmasq.d: dnsmasq lee TODOS los
# ficheros del directorio y una copia duplica cada palabra clave.
for f in /etc/dnsmasq.d/*.bak-*; do
	[ -e "$f" ] || continue
	mv "$f" "$BKP/"
done
if [ -f "$CFG" ]; then
	cp -a "$CFG" "$BKP/varec-spe.conf.bak-$STAMP"
fi

cat > "$CFG" <<'EOF'
interface=pcie0
bind-interfaces
# port=0 -> sin servidor DNS. Aqui solo repartimos direcciones.
port=0

# ⚠️ "static": NO se reparte del monton. Solo se sirve a las MACs nominadas
# abajo. Es lo que impide volver a dar configuracion a equipos ajenos.
dhcp-range=192.168.50.10,static,255.255.255.0,12h
dhcp-ignore=tag:!known

# Una linea por placa, con sus dos MAC (puerto 1 y puerto 2).
dhcp-host=02:00:70:22:30:5a,02:00:70:22:30:5b,192.168.50.11,att-tank1
dhcp-host=02:00:70:2d:30:08,02:00:70:2d:30:09,192.168.50.21,att-tank21

dhcp-option=option:router,192.168.50.1
# Mejor no anunciar DNS que anunciar uno que no responde.
dhcp-option=option:dns-server

log-dhcp
EOF
echo "  escrito $CFG"

# La MISMA prueba que hace el ExecStartPre de Debian: -7 sobre el directorio
# entero. `dnsmasq --test` a secas da falsos OK.
echo "  comprobando como lo hace systemd..."
if ! /usr/sbin/dnsmasq --test -7 /etc/dnsmasq.d,.dpkg-dist,.dpkg-old,.dpkg-new; then
	echo "  >>> configuracion INVALIDA: no se reinicia el servicio." >&2
	exit 1
fi

systemctl restart dnsmasq
sleep 2
echo "  dnsmasq: $(systemctl is-active dnsmasq)"

echo
echo "===== 3) estado ====="
cat /var/lib/misc/dnsmasq.leases 2>/dev/null || echo "  (sin concesiones todavia)"
echo
echo "===== LISTO ====="
echo "Direcciones reservadas:  tank1 -> 192.168.50.11   tank21 -> 192.168.50.21"
echo "Dale un ciclo de tension a la ATT: su pila de red esta muerta y ademas"
echo "tiene que pedir direccion nueva."
