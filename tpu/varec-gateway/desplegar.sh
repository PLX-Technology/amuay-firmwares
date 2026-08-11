#!/bin/bash
# Despliegue de la pasarela Varec. Se ejecuta con sudo:
#
#     ssh -t tpu01@<ip> sudo bash /home/tpu01/despliegue/desplegar.sh
#
# ⚠️ NO EDITAR NI COPIAR NADA EN $SRC MIENTRAS ESTO CORRE.
# El 2026-08-04 un scp en vuelo coincidio con este script y dejo los CUATRO
# ficheros de /opt A CERO BYTES: scp trunca el destino antes de escribirlo, asi
# que se instalaron ficheros vacios. La pasarela se quedo en bucle de reinicio
# y la copia de seguridad NO SIRVIO -- se habia hecho del /opt ya vaciado por
# un despliegue anterior con el mismo problema.
set -e

SRC=/home/tpu01/despliegue
DST=/opt/varec-gateway
BAK=/var/backups/varec-gateway/$(date +%Y%m%d-%H%M%S)
# ⚠️ UN FICHERO QUE NO ESTE EN ESTA LISTA NO SE DESPLIEGA, y no avisa de nada:
# la version vieja se queda en /opt y el import falla, o -- peor -- funciona
# con el modulo anterior y uno cree estar probando el codigo nuevo.
FICHEROS="gateway.py outputs.py webui.py sensor.py ota.py"

# --- 1) Validar el ORIGEN antes de tocar nada -----------------------------
# ⚠️ Esta es la comprobacion que faltaba. Un fichero vacio o con sintaxis rota
# no debe llegar nunca a /opt: mejor abortar aqui, con todo intacto, que
# dejar la pasarela caida y sin vuelta atras.
echo "== validando origen"
for f in $FICHEROS; do
    if [ ! -s "$SRC/$f" ]; then
        echo "!! $SRC/$f esta VACIO o no existe. Abortado, no se toca /opt."
        echo "!! (¿un scp a medias? volver a copiarlo y reintentar)"
        exit 1
    fi
done
python3 - "$SRC" $FICHEROS <<'PYEOF' || { echo "!! sintaxis invalida. Abortado."; exit 1; }
import ast, sys
d = sys.argv[1]
for f in sys.argv[2:]:
    ast.parse(open(d + "/" + f, encoding="utf-8").read())
    print("   OK %s" % f)
PYEOF

# ⚠️ webui.py GENERA JavaScript. Que el Python sea valido no dice NADA del JS
# que lleva dentro: el 2026-08-04 se desplegaron cadenas JS partidas por saltos
# de linea reales y el panel se quedo en "cargando..." con todo el script
# muerto. Se busca la senal mas barata de eso.
python3 - "$SRC/webui.py" <<'PYEOF' || { echo "!! JS sospechoso. Abortado."; exit 1; }
import re, sys
js = re.search(r"<script>(.*?)</script>", open(sys.argv[1], encoding="utf-8").read(), re.S)
if not js:
    print("   (sin bloque <script>, nada que comprobar)"); raise SystemExit
malas = [i for i, l in enumerate(js.group(1).splitlines(), 1)
         if not l.strip().startswith("//") and l.count("'") % 2]
if malas:
    print("   !! cadenas JS sin cerrar en las lineas: %s" % malas[:8]); raise SystemExit(1)
print("   OK javascript del panel")
PYEOF

# --- 2) Copia de seguridad, solo si /opt tiene algo que salvar -------------
echo
echo "== copia de seguridad"
vacio=1
for f in $FICHEROS; do [ -s "$DST/$f" ] && vacio=0; done
if [ "$vacio" = "1" ]; then
    echo "   /opt ya estaba vacio: NO se hace copia (guardaria basura y"
    echo "   sobrescribiria la ultima copia buena)"
else
    install -d "$BAK"
    # ⚠️ SOLO LOS QUE YA EXISTEN EN /opt. Un modulo NUEVO todavia no esta ahi y
    # no hay nada suyo que salvar; copiarlo a ciegas hacia fallar `cp`, y con
    # `set -e` eso abortaba el despliegue entero. Paso el 2026-08-11 al anadir
    # ota.py a la lista.
    for f in $FICHEROS; do
        [ -e "$DST/$f" ] && cp -a "$DST/$f" "$BAK/"
    done
    echo "   guardada en $BAK"
fi

# --- 3) Instalar y reiniciar ----------------------------------------------
echo
echo "== instalando"
for f in $FICHEROS; do install -m 755 -o root -g root "$SRC/$f" "$DST/"; done

echo "== reiniciando"
systemctl restart varec-gateway
sleep 4
if ! systemctl is-active --quiet varec-gateway; then
    echo "!! NO ARRANCO -- ultimas lineas del diario:"
    journalctl -u varec-gateway -n 25 --no-pager
    [ -d "$BAK" ] && echo "!! revertir:  sudo cp -a $BAK/*.py $DST/ && sudo systemctl restart varec-gateway"
    exit 1
fi
echo "   activo"

echo
echo "== /api/switches"; curl -s --max-time 5 http://127.0.0.1:8080/api/switches | head -c 400; echo
echo "== /api/pse";      curl -s --max-time 5 http://127.0.0.1:8080/api/pse      | head -c 200; echo
[ -d "$BAK" ] && echo && echo "Para revertir:  sudo cp -a $BAK/*.py $DST/ && sudo systemctl restart varec-gateway"
