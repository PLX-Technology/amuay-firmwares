#!/usr/bin/env python3
"""Diagnostica por que un consumidor de SOLO LECTURA no recibe tramas.

    python3 probar_ro.py [usuario]        # por defecto varec-ro

Un cliente MQTT que no recibe nada tiene al menos tres causas distintas y
NINGUNA se distingue de las otras mirando el cliente, porque las tres se ven
igual: silencio.

  1. El usuario no existe o la clave no es esa  -> CONNACK rc=5
  2. La ACL no le deja leer ese topico          -> SUBACK con QoS 128
  3. Esta suscrito a un topico que nadie publica -> todo correcto, cero mensajes

Esto las separa: informa del rc de conexion, del QoS concedido a CADA
suscripcion, y de que llega en una ventana de escucha. Sin esto hay que
adivinar, y el 2026-08-06 se perdio un rato largo confundiendo el caso 2 con
"la pasarela no publica".

La clave se teclea (o va en VAREC_RO_PASS): no queda en el historial ni en la
lista de procesos. No hace falta sudo.
"""
import getpass, json, os, sys, time
import yaml
import paho.mqtt.client as mqtt

cfg = (yaml.safe_load(open("/etc/varec-gateway/config.yaml")) or {}).get("mqtt") or {}
pref = cfg.get("topic_prefix", "varec")
host, port = cfg.get("host", "localhost"), cfg.get("port", 1883)

usuario = sys.argv[1] if len(sys.argv) > 1 else "varec-ro"
clave = os.environ.get("VAREC_RO_PASS") or getpass.getpass("  clave de %s: " % usuario)

# Se prueban por separado el comodin y los topicos concretos: si el comodin
# fallara y los concretos no (o al reves) el sintoma senala directamente a la
# regla de la ACL que hay que mirar.
TOPICOS = ["#", "$SYS/#", pref + "/#", pref + "/topologia",
           pref + "/gateway/status", pref + "/+/level"]
# `#` y `$SYS/#` estan a proposito los primeros: son los que usan POR DEFECTO
# casi todas las herramientas graficas (MQTT Explorer, MQTTX...), y la ACL de
# varec-ro solo concede varec/#. Suscribirse a `#` se DENIEGA entero -- no se
# recorta a lo permitido -- asi que el cliente se queda mudo aunque el usuario
# y la clave sean correctos y la pasarela este publicando con normalidad.

estado = {"rc": None, "suback": {}, "msgs": {}, "primero": None}
mid2top = {}


def on_connect(c, u, f, rc):
    estado["rc"] = rc
    if rc != 0:
        return
    for t in TOPICOS:
        r, mid = c.subscribe(t, qos=0)
        mid2top[mid] = t


def on_subscribe(c, u, mid, qos):
    estado["suback"][mid2top.get(mid, "?")] = list(qos)


def on_message(c, u, msg):
    estado["msgs"][msg.topic] = estado["msgs"].get(msg.topic, 0) + 1
    if estado["primero"] is None:
        estado["primero"] = (msg.topic, msg.payload[:120])


c = mqtt.Client()
c.username_pw_set(usuario, clave)
c.on_connect, c.on_subscribe, c.on_message = on_connect, on_subscribe, on_message
try:
    c.connect(host, port, 30)
except Exception as e:
    print("  ⛔ no se pudo abrir la conexion a %s:%s -- %s" % (host, port, e))
    sys.exit(1)
c.loop_start()

VENTANA = 15
print("  broker: %s:%s   usuario: %s" % (host, port, usuario))
time.sleep(2)

RC = {0: "conectado", 1: "protocolo no aceptado", 2: "client id rechazado",
      3: "broker no disponible", 4: "usuario o clave incorrectos",
      5: "no autorizado"}
print("  conexion: rc=%s (%s)" % (estado["rc"], RC.get(estado["rc"], "?")))
if estado["rc"] != 0:
    print()
    print("  ⛔ El broker NO acepta a este usuario. O no existe en")
    print("     /etc/mosquitto/passwd, o la clave no es esa. Para crearlo:")
    print("       sudo mosquitto_passwd /etc/mosquitto/passwd %s" % usuario)
    print("       sudo systemctl reload mosquitto")
    sys.exit(1)

print("  suscripciones (QoS 128 = DENEGADA por la ACL):")
denegadas = []
for t in TOPICOS:
    q = estado["suback"].get(t)
    marca = "?" if q is None else ("DENEGADA" if 128 in q else "ok qos=%d" % q[0])
    if q is not None and 128 in q:
        denegadas.append(t)
    print("    %-24s %s" % (t, marca))

print()
print("  escuchando %d s..." % VENTANA)
time.sleep(VENTANA)
c.loop_stop()

total = sum(estado["msgs"].values())
print("  mensajes recibidos: %d en %d topicos" % (total, len(estado["msgs"])))
for t, n in sorted(estado["msgs"].items(), key=lambda x: -x[1])[:12]:
    print("    %-34s %d" % (t, n))

print()
if denegadas and total > 0:
    print("  ✔ %s RECIBE, pero la ACL le deniega: %s" % (usuario, ", ".join(denegadas)))
    print()
    print("  ★ SI TU CLIENTE NO VE NADA, ES POR ESTO: la herramienta se suscribe")
    print("    a `#` por defecto y la ACL solo concede %s/#. Mosquitto DENIEGA la" % pref)
    print("    suscripcion entera -- no la recorta a lo permitido -- y el cliente")
    print("    se queda mudo sin dar ningun error.")
    print("    Solucion: suscribirse a  %s/#  en vez de a  #" % pref)
elif denegadas:
    print("  ⛔ La ACL le DENIEGA a %s: %s" % (usuario, ", ".join(denegadas)))
    print("     Mira la seccion 'user %s' de /etc/mosquitto/acl." % usuario)
elif total == 0:
    print("  ⛔ Conecta y le dejan suscribirse, pero NO LLEGA NADA.")
    print("     Entonces el problema no es el consumidor: es que la pasarela no")
    print("     esta publicando. Comprueba que su cliente MQTT sigue conectado:")
    print("       ss -tn | grep ':%s'" % port)
else:
    print("  ✔ %s recibe con normalidad." % usuario)
    if estado["primero"]:
        print("     primero: %s -> %s" % (estado["primero"][0],
                                          estado["primero"][1].decode("utf-8", "replace")))
