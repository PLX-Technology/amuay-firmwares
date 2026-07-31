#!/bin/bash
# Prueba de integridad del NVMe: escribe, RELEE SIN CACHE y compara.
#
# Por que asi y no con dd a secas: si se relee por la cache de pagina no se
# toca el disco y la prueba no vale nada. Con iflag=direct (O_DIRECT) se
# fuerza la lectura del dispositivo, y no hace falta root para tirar caches.
#
# Que busca:
#   - corrupcion silenciosa  -> el sha256 releido no coincide
#   - caida del enlace PCIe   -> errores de E/S, o el dispositivo desaparece
#
# Antecedente (2026-07-30): a Gen2 el disco desaparecia a los ~201 s con E/S
# activa; a Gen1 aguanto mas de 10 min. Este test debe correr el tiempo
# suficiente para cruzar ese umbral con margen.
#
# Uso:  nvme_integridad.sh [minutos] [MB_por_pasada]

set -u
DIR=/mnt/ssd/_integridad
MIN=${1:-15}
MB=${2:-512}
LOG=/mnt/ssd/nvme_integridad.log

mkdir -p "$DIR" || exit 1
exec >>"$LOG" 2>&1

echo "===== inicio $(date -Is)  duracion=${MIN}min  bloque=${MB}MB ====="
echo "velocidad de enlace al empezar:"
for d in 0001:02:07.0 0001:04:00.0; do
	printf '  %-14s %s\n' "$d" "$(cat /sys/bus/pci/devices/$d/current_link_speed 2>/dev/null || echo AUSENTE)"
done

# Patron de origen, generado una sola vez: /dev/urandom seria el cuello de
# botella y estariamos midiendo la CPU, no el disco.
SRC=$DIR/patron.bin
if [ ! -f "$SRC" ]; then
	dd if=/dev/urandom of="$SRC" bs=1M count="$MB" status=none || { echo "FALLO creando el patron"; exit 1; }
fi
SHA_SRC=$(sha256sum "$SRC" | cut -d' ' -f1)
echo "patron: ${MB}MB sha=${SHA_SRC:0:16}"

FIN=$(( $(date +%s) + MIN*60 ))
N=0
ERR=0
while [ "$(date +%s)" -lt "$FIN" ]; do
	N=$((N+1))
	F=$DIR/prueba_$((N % 4)).bin

	if ! dd if="$SRC" of="$F" bs=1M oflag=direct status=none; then
		echo "$(date -Is)  pasada $N: FALLO DE ESCRITURA"; ERR=$((ERR+1))
		[ -e /dev/nvme0n1 ] || echo "  >>> /dev/nvme0n1 HA DESAPARECIDO"
		break
	fi
	sync

	SHA=$(dd if="$F" bs=1M iflag=direct status=none | sha256sum | cut -d' ' -f1)
	if [ "$SHA" != "$SHA_SRC" ]; then
		echo "$(date -Is)  pasada $N: ★ CORRUPCION  leido=${SHA:0:16} esperado=${SHA_SRC:0:16}"
		ERR=$((ERR+1))
		break
	fi

	SPD=$(cat /sys/bus/pci/devices/0001:04:00.0/current_link_speed 2>/dev/null || echo AUSENTE)
	echo "$(date -Is)  pasada $N OK  (t+$(( $(date +%s) - (FIN - MIN*60) ))s)  enlace=$SPD"
done

echo "===== fin $(date -Is)  pasadas=$N  errores=$ERR ====="
if [ "$ERR" -eq 0 ]; then echo "RESULTADO: SIN ERRORES"; else echo "RESULTADO: FALLO"; fi
rm -f "$DIR"/prueba_*.bin
