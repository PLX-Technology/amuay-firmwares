#!/bin/bash
# Hace que la pasarela escriba al journal EN EL MOMENTO, no al terminar.
#
#     sudo bash sin_buffer.sh
#
# Python bufferiza stdout cuando no va a un terminal, asi que bajo systemd los
# print() de la pasarela se quedan en memoria y solo se vuelcan al PARAR el
# servicio. El efecto practico es desconcertante: en `journalctl` los mensajes
# de arranque aparecen con la marca de tiempo de la PARADA, y una linea como
#
#     [mqtt] orden de calibracion -> rechazada
#
# no se ve nunca mientras el servicio esta vivo. El 2026-08-06 eso dejo sin
# forma de comprobar si una orden de calibracion llegaba: hubo que deducirlo de
# la ACL en vez de leerlo del log.
#
# PYTHONUNBUFFERED=1 lo arregla y no tiene coste apreciable: son unas pocas
# lineas por minuto, no un bucle de escritura.
set -e

U=/etc/systemd/system/varec-gateway.service
[ -f "$U" ] || U=$(systemctl show -p FragmentPath --value varec-gateway)
echo "== unidad: $U"

if grep -q 'PYTHONUNBUFFERED' "$U"; then
    echo "   ya lo tiene, no se toca"
    exit 0
fi

R="${U}.bak-$(date +%Y%m%d-%H%M%S)"
cp -a "$U" "$R"
echo "   respaldo: $R"

# Con python y no con sed: hay que insertar DENTRO de [Service] y antes del
# ExecStart, y encadenar sed para eso es justo como se estropea un fichero de
# unidad. Si algo no cuadra, se aborta y el respaldo sigue en su sitio.
python3 - "$U" <<'PYEOF' || { echo "!! no se pudo editar la unidad. Abortado."; exit 1; }
import sys
p = sys.argv[1]
ls = open(p, encoding="utf-8").read().splitlines(True)
try:
    i = next(n for n, l in enumerate(ls) if l.strip().startswith("ExecStart="))
except StopIteration:
    print("   no encuentro ExecStart="); raise SystemExit(1)
if not any(l.strip() == "[Service]" for l in ls[:i]):
    print("   ExecStart= no esta bajo [Service]"); raise SystemExit(1)
ls.insert(i, "Environment=PYTHONUNBUFFERED=1\n")
open(p, "w", encoding="utf-8").writelines(ls)
print("   insertado Environment=PYTHONUNBUFFERED=1 en la linea %d" % (i + 1))
PYEOF
grep -nE '^\[|^Environment=|^ExecStart=' "$U" | sed 's/^/    /'
systemd-analyze verify "$U" 2>&1 | sed 's/^/    /' || true

systemctl daemon-reload
systemctl restart varec-gateway
sleep 3
systemctl is-active varec-gateway

echo
echo "== el log de ahora mismo (antes salia vacio hasta parar el servicio)"
journalctl -u varec-gateway --since "-1 min" --no-pager | tail -12
