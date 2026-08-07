#!/usr/bin/env python3
"""Prueba el camino de ordenes de calibracion por MQTT, sin tocar nada real.

    python3 probar_cmd_mqtt.py [usuario]        # por defecto varec-sala

★ SE CONECTA COMO LA SALA (`varec-sala`), NO como la pasarela (`varec`).

No es un detalle: la ACL le da al usuario de la pasarela `topic write varec/#`
pero solo `topic read varec/cmd/calibrar`. Con esa credencial la orden SALE
pero la respuesta NO SE PUEDE LEER -- el broker deniega la suscripcion a
varec/cmd/resultado en silencio, y la prueba dice "SIN RESPUESTA" como si el
camino estuviera roto cuando lo unico roto era la prueba. Paso exactamente eso
el 2026-08-06.

Probar con la credencial de la sala ademas VALIDA LA ACL en lugar de
esquivarla: si la sala no puede ordenar o no puede leer la respuesta, esto se
entera aqui y no el dia que haga falta calibrar un tanque.

La clave se teclea (o va en VAREC_SALA_PASS): asi no queda en el historial del
shell ni en la lista de procesos. Y por eso ya NO hace falta sudo.
"""
import getpass, json, os, sys, time
import yaml
import paho.mqtt.client as mqtt

cfg = (yaml.safe_load(open("/etc/varec-gateway/config.yaml")) or {}).get("mqtt") or {}

usuario = sys.argv[1] if len(sys.argv) > 1 else "varec-sala"
clave = os.environ.get("VAREC_SALA_PASS")
if not clave:
    clave = getpass.getpass("  clave de %s: " % usuario)

pref = cfg.get("topic_prefix", "varec")
T_CMD = cfg.get("cmd_topic") or pref + "/cmd/calibrar"
T_RES = cfg.get("res_topic") or pref + "/cmd/resultado"
recibidas = []
suscrito = []
rechazo = []

def on_connect(c, u, f, rc):
    # rc 5 = no autorizado. Sin esto un usuario o clave mal escritos se ven
    # igual que un camino roto: silencio.
    if rc != 0:
        rechazo.append(rc); return
    c.subscribe(T_RES, qos=1)

def on_subscribe(c, u, mid, qos):
    # qos 128 = el broker DENIEGA la suscripcion. Es el sintoma exacto de una
    # ACL que no deja leer las respuestas, y hay que decirlo en voz alta
    # porque el cliente no da ningun otro aviso.
    suscrito.append(all(q != 128 for q in qos))

def on_message(c, u, msg):
    recibidas.append(json.loads(msg.payload.decode()))

c = mqtt.Client()
c.username_pw_set(usuario, clave)
c.on_connect, c.on_message, c.on_subscribe = on_connect, on_message, on_subscribe
c.connect(cfg.get("host", "localhost"), cfg.get("port", 1883), 30)
c.loop_start(); time.sleep(1.5)

if rechazo:
    print("  ⛔ el broker rechazo la conexion de %s (rc=%d): usuario o clave"
          % (usuario, rechazo[0]))
    sys.exit(1)
if suscrito and not suscrito[0]:
    print("  ⛔ el broker DENIEGA la suscripcion de %s a %s." % (usuario, T_RES))
    print("     La ACL no le deja leer las respuestas. Revisa acl_mqtt.sh.")
    sys.exit(1)
if not suscrito:
    print("  ⛔ sin confirmacion de suscripcion a %s" % T_RES)
    sys.exit(1)
print("  conectado como %s y suscrito a %s" % (usuario, T_RES))

# ⚠️ TODAS las pruebas son ordenes que deben RECHAZARSE. Es a proposito: una
# orden valida escribiria una calibracion de verdad en un tanque de verdad, y
# una prueba no puede tener ese efecto. Lo que se comprueba aqui es el CAMINO
# (llega, se valida, responde), no la aritmetica -- esa va en la propia
# pasarela.
pruebas = [
    ("dos puntos: puntos iguales",
     {"id": "prueba-1", "tank_id": 21, "unidad": "mm",
      "punto_a": {"pulsos": 1000, "nivel": 500},
      "punto_b": {"pulsos": 1000, "nivel": 900}}),
    ("dos puntos: orden mal formada",
     {"id": "prueba-2", "tank_id": 21, "punto_a": {"pulsos": 1}}),
    ("geometrica: sin altura_referencia",
     {"id": "prueba-3", "tank_id": 21, "modo": "geometrica",
      "mm_por_pulso": 0.595312}),
    ("geometrica: mm_por_pulso negativo",
     {"id": "prueba-4", "tank_id": 21, "modo": "geometrica",
      "mm_por_pulso": -0.595312, "altura_referencia": 12000}),
    ("geometrica: sentido invalido",
     {"id": "prueba-5", "tank_id": 21, "modo": "geometrica",
      "mm_por_pulso": 0.595312, "altura_referencia": 12000,
      "sentido": "lateral"}),
]

# La orden que SI calibra, para copiar y pegar cambiando el tanque y la altura.
# 0,595312 = 304,8 mm por vuelta (cabezal ingles) / 512 pulsos.
EJEMPLO = {"id": "sala-001", "tank_id": 1, "modo": "geometrica",
           "mm_por_pulso": 0.595312, "altura_referencia": 12000,
           "sentido": "baja", "unidad": "mm"}
print("  topico de ordenes:   %s" % T_CMD)
print("  topico de resultado: %s" % T_RES)
print()
for nombre, orden in pruebas:
    n = len(recibidas)
    c.publish(T_CMD, json.dumps(orden), qos=1)
    for _ in range(50):
        if len(recibidas) > n: break
        time.sleep(0.1)
    if len(recibidas) > n:
        r = recibidas[-1]
        print("  %-38s -> %s" % (nombre, r.get("estado")))
        print("      motivo: %s" % r.get("motivo"))
        print("      id devuelto: %s" % r.get("id"))
    else:
        print("  %-38s -> SIN RESPUESTA" % nombre)
        print("      la orden salio pero no volvio nada: o la pasarela no esta")
        print("      suscrita a %s, o la ACL no deja publicar ahi" % T_CMD)
c.loop_stop()
print()
print("  ✔ Si TODAS salen 'rechazada' CON motivo, el camino esta completo:")
print("    la orden llega, se valida y se responde.")
print()
print("  Para calibrar de verdad, publica en %s algo asi:" % T_CMD)
print("  " + json.dumps(EJEMPLO))
