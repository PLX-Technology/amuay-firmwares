#!/bin/bash
# Instala el forzado a Gen1 al arranque y mueve la base de la pasarela al M.2.
#
# Se ejecuta con sudo, UNA vez. Es idempotente: se puede repetir sin romper nada.
#
# QUE HACE, y por que en este orden:
#   1) Instala /usr/local/sbin/nvme-gen1.sh y su servicio, y lo habilita.
#      Primero esto: si el disco arranca a Gen2 y algo escribe fuerte, se cae.
#   2) PARA la pasarela antes de tocar la base. Copiar un SQLite con un
#      escritor vivo es la forma clasica de llevarse una copia corrupta.
#   3) Copia varec.db Y SUS ACOMPANANTES (-wal, -shm). En modo WAL los datos
#      recientes viven en el -wal; copiar solo el .db pierde lo ultimo.
#   4) Cambia store.path en el config al M.2.
#   5) Arranca y comprueba.
#
# La unidad del servicio YA trae RequiresMountsFor=/mnt/ssd, asi que si el SSD
# no monta, la pasarela no arranca en vez de crear una base vacia en un
# directorio que luego queda oculto bajo el punto de montaje.

set -euo pipefail

SRC_DIR=/var/lib/varec-gateway
DST_DIR=/mnt/ssd/varec-gateway
CFG=/etc/varec-gateway/config.yaml
STAMP=$(date +%Y%m%d-%H%M%S)

echo "===== 1) Gen1 al arranque ====="
install -m 0755 "$(dirname "$0")/nvme-gen1.sh" /usr/local/sbin/nvme-gen1.sh
install -m 0644 "$(dirname "$0")/nvme-gen1.service" /etc/systemd/system/nvme-gen1.service
systemctl daemon-reload
systemctl enable nvme-gen1.service
systemctl start  nvme-gen1.service || true
echo "--- estado ---"
systemctl is-enabled nvme-gen1.service || true
journalctl -u nvme-gen1.service -n 5 --no-pager || true

echo
echo "===== 2) parar la pasarela ====="
systemctl stop varec-gateway.service
sleep 2

echo
echo "===== 3) copiar la base al M.2 ====="
mkdir -p "$DST_DIR"
if [ -f "$SRC_DIR/varec.db" ]; then
	# El -wal es imprescindible: en modo WAL lo reciente vive ahi.
	for f in varec.db varec.db-wal varec.db-shm; do
		[ -f "$SRC_DIR/$f" ] && cp -a "$SRC_DIR/$f" "$DST_DIR/$f" && echo "  copiado $f"
	done
	# Respaldo del original, por si acaso. No se borra.
	mv "$SRC_DIR" "${SRC_DIR}.bak-$STAMP"
	echo "  original conservado en ${SRC_DIR}.bak-$STAMP"
else
	echo "  (no habia base previa)"
fi

echo
echo "===== 4) apuntar el config al M.2 ====="
cp -a "$CFG" "$CFG.bak-$STAMP"
sed -i "s#^\(\s*path:\s*\).*varec\.db#\1$DST_DIR/varec.db#" "$CFG"
grep -A3 '^store:' "$CFG"

echo
echo "===== 5) arrancar y comprobar ====="
systemctl start varec-gateway.service
sleep 5
systemctl is-active varec-gateway.service
echo "--- ficheros en el M.2 ---"
ls -l "$DST_DIR"
echo "--- velocidad del enlace ---"
NV=$(basename "$(readlink -f /sys/class/nvme/nvme0/device)")
cat "/sys/bus/pci/devices/$NV/current_link_speed"
echo
echo "===== LISTO ====="
