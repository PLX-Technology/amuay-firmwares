#!/bin/bash
# Mueve el plano IP del segmento SPE de pcie0 a eth0.
#
# POR QUE
# El cable del RJ45 del MPS esta en eth0 (confirmado por el usuario, y
# corroborado: eth0 enlaza a 100 Mbit, que es lo que da el ADIN1300 del MPS
# en RMII; pcie0 enlaza a 1000, que el MPS no puede negociar).
#
# dnsmasq atado a pcie0 RECIBE la peticion de la ATT pero manda la respuesta
# solo por pcie0. Por eso se veia DHCPOFFER en el registro y nunca DHCPACK:
# la oferta salia por el puerto equivocado.
#
# ⚠️ pcie0 se queda SIN direccion y no pasa nada: la pasarela lee con un
# socket AF_PACKET (crudo), que no necesita IP. Solo el canal de
# configuracion (DHCP + Modbus TCP) necesita direccionamiento.
#
# Reversible: `nmcli con up varec-spe` devuelve la IP a pcie0.

set -euo pipefail

STAMP=$(date +%Y%m%d-%H%M%S)
CFG=/etc/dnsmasq.d/varec-spe.conf
BKP=/var/backups/varec-spe
mkdir -p "$BKP"

echo "===== 1) quitar la IP de pcie0 ====="
# Se baja el perfil, no se borra: asi volver atras es un solo comando.
nmcli connection down varec-spe >/dev/null 2>&1 || true
echo "  perfil 'varec-spe' (pcie0) desactivado; sigue existiendo"
ip -br addr show pcie0

echo
echo "===== 2) IP del segmento SPE en eth0 ====="
if nmcli -t -f NAME con show | grep -qx "varec-spe-eth0"; then
	echo "  el perfil ya existe: se reutiliza"
else
	nmcli connection add type ethernet ifname eth0 con-name varec-spe-eth0 \
		ipv4.method manual ipv4.addresses 192.168.50.1/24 \
		ipv4.never-default yes ipv6.method disabled \
		connection.autoconnect yes >/dev/null
	echo "  perfil 'varec-spe-eth0' creado"
fi
nmcli connection up varec-spe-eth0 >/dev/null
ip -br addr show eth0

echo
echo "===== 3) dnsmasq escuchando en eth0 ====="
cp -a "$CFG" "$BKP/varec-spe.conf.bak-$STAMP"
sed -i 's/^interface=pcie0$/interface=eth0/' "$CFG"
grep -E "^interface=" "$CFG"

if ! /usr/sbin/dnsmasq --test -7 /etc/dnsmasq.d,.dpkg-dist,.dpkg-old,.dpkg-new; then
	echo "  >>> configuracion INVALIDA: no se reinicia." >&2
	exit 1
fi
systemctl restart dnsmasq
sleep 2
echo "  dnsmasq: $(systemctl is-active dnsmasq)"

echo
echo "===== 4) estado ====="
echo "--- concesiones (la ATT tarda: reintenta con espaciado creciente) ---"
cat /var/lib/misc/dnsmasq.leases 2>/dev/null || echo "  (vacio todavia)"
echo
echo "===== LISTO ====="
echo "Si en un par de minutos no hay concesion, un reset de la ATT la fuerza"
echo "a pedir de inmediato."
