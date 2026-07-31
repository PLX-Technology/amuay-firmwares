#!/bin/sh
# Fuerza el enlace PCIe del NVMe a Gen1 (2.5 GT/s).
#
# POR QUE
# A Gen2 (5 GT/s) el disco se cae bajo E/S sostenida: medido el 2026-07-31, el
# controlador quedo `dead` a los ~7 min con el sistema de ficheros remontado en
# solo lectura ("Unable to change power state from D3cold to D0"). A Gen1
# aguanto 20 min y 211 pasadas de escritura+relectura verificada sin un error.
#
# NO CUESTA ANCHO DE BANDA: el tramo de subida CM5<->ASM1182e YA negocia a
# 2.5 GT/s, asi que el cuello de botella no se mueve. Medido: mismo tiempo por
# pasada a Gen1 que a Gen2.
#
# Este ajuste NO sobrevive a un reinicio por si solo -- de ahi este servicio.
#
# El puerto se descubre a partir del propio NVMe en vez de codificarlo: los
# BDF pueden cambiar si se reordena el arbol PCIe.

set -u

SYS=/sys/class/nvme/nvme0/device
[ -e "$SYS" ] || { echo "nvme0 no existe; nada que hacer"; exit 0; }

DEV=$(readlink -f "$SYS")            # .../0001:02:07.0/0001:04:00.0
NVME=$(basename "$DEV")              # 0001:04:00.0
PORT=$(basename "$(dirname "$DEV")") # 0001:02:07.0  = puerto descendente del switch

ANTES=$(cat "/sys/bus/pci/devices/$NVME/current_link_speed" 2>/dev/null || echo "?")
echo "NVMe=$NVME  puerto=$PORT  velocidad antes: $ANTES"

case "$ANTES" in
	"2.5 GT/s"*) echo "ya esta en Gen1; no se toca"; exit 0 ;;
esac

# Target Link Speed = bits 3:0 del Link Control 2 (CAP_EXP + 0x30). La mascara
# deja intacto el resto del registro.
setpci -s "$PORT" CAP_EXP+30.w=0001:000f || { echo "FALLO escribiendo Link Control 2"; exit 1; }

# Retrain Link = bit 5 del Link Control (CAP_EXP + 0x10)
setpci -s "$PORT" CAP_EXP+10.w=0020:0020 || { echo "FALLO pidiendo el retrain"; exit 1; }

# El reentrenamiento no es instantaneo
i=0
while [ $i -lt 20 ]; do
	sleep 0.1
	AHORA=$(cat "/sys/bus/pci/devices/$NVME/current_link_speed" 2>/dev/null || echo "?")
	case "$AHORA" in "2.5 GT/s"*) break ;; esac
	i=$((i+1))
done

echo "velocidad despues: $AHORA"
case "$AHORA" in
	"2.5 GT/s"*) echo "OK: enlace del NVMe a Gen1"; exit 0 ;;
	*) echo "AVISO: no se pudo bajar a Gen1 (sigue en $AHORA)"; exit 1 ;;
esac
