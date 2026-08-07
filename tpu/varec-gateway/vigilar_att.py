#!/usr/bin/env python3
"""Vigila QUE hace una ATT al arrancar, con marca de tiempo, y la rescata.

    python3 vigilar_att.py <mac> <ip> [tank_id]
    python3 vigilar_att.py 02:00:70:22:30:66 192.168.50.7 7

Por que existe: una placa que no aparece admite varias explicaciones que desde
fuera se ven IGUAL (silencio), y hasta ahora las estabamos distinguiendo por
deduccion. Esto las separa por observacion, midiendo CUATRO cosas a la vez y
apuntando el instante exacto en que cada una cambia:

  potencia   el puerto del field switch: entrega o no, y cuantos mA
  dhcp       si la placa PIDE direccion (log de dnsmasq)
  modbus     si su puerto 502 acepta conexion (o sea: pila IP arriba)
  tramas     si la pasarela ve sus tramas L2 (aparece en `nuevas`)

Asi se puede decir cual de estas es, en vez de suponer:

  ni potencia .............. cable, conector o negociacion
  potencia y nada mas ...... arranca y se suspende antes de levantar la red
  dhcp pero no modbus ...... levanta la red y se suspende, o no le dan IP
  modbus un rato y calla ... se suspende DESPUES; la ventana se mide aqui

Y en cuanto el Modbus responde, aprovecha la ventana: apaga CFG_F_BAT y escribe
el tank_id, que es justo lo que no da tiempo a hacer a mano.
"""
import json, re, socket, struct, subprocess, sys, time
import urllib.request

MAC = sys.argv[1].lower()
IP = sys.argv[2]
TANK = int(sys.argv[3]) if len(sys.argv) > 3 else None
F_BAT = 0x10
T0 = time.time()


def t():
    return "[%6.1fs]" % (time.time() - T0)


def dice(que):
    print("%s %s" % (t(), que), flush=True)   # flush: esto se mira EN VIVO


# ---------------------------------------------------------------- modbus
def txn(sock, pdu):
    sock.sendall(struct.pack(">HHHB", 1, 0, len(pdu) + 1, 1) + pdu)
    hdr = sock.recv(7)
    if len(hdr) < 7:
        raise IOError("respuesta corta")
    ln = struct.unpack(">H", hdr[4:6])[0]
    body = b""
    while len(body) < ln - 1:
        trozo = sock.recv(ln - 1 - len(body))
        if not trozo:
            raise IOError("cerrada")
        body += trozo
    if body[0] & 0x80:
        raise IOError("excepcion 0x%02x" % body[1])
    return body


def leer(sock, func, addr, n):
    b = txn(sock, struct.pack(">BHH", func, addr, n))
    return struct.unpack(">%dH" % (b[1] // 2), b[2:2 + b[1]])


def rescatar(sock):
    """Le escribe su identidad. NO le toca las banderas.

    ⚠️ Antes esto APAGABA CFG_F_BAT, y era un error mio: la apagaba creyendo
    que era la causa de que la placa estuviera muda. Pero esa bandera viene
    ENCENDIDA DE FABRICA a proposito (decision del 2026-08-06), asi que
    apagarla convertia una puesta en marcha de un paso en una de dos --
    escribir el tank_id y despues volver a encender lo que este script acababa
    de apagar. Una placa virgen ya llega con las banderas correctas: 0x13.

    Si alguna placa concreta necesita la bandera apagada (PB11 invertido, caso
    medido en tank21), eso es una decision POR UNIDAD y se hace a mano, no
    desde una herramienta de alta que corre en cada instalacion.
    """
    flags = leer(sock, 3, 0, 1)[0]
    dice("    HR 0 (banderas) = 0x%04X   CFG_F_BAT %s"
         % (flags, "ENCENDIDA" if flags & F_BAT else "apagada"))
    try:
        pwr = leer(sock, 4, 12, 1)[0]
        dice("    IR 12 = 0x%04X -> pb11=%d en_bateria=%d bat_activa=%d gpio_listo=%d"
             % (pwr, pwr & 1, (pwr >> 1) & 1, (pwr >> 2) & 1, (pwr >> 3) & 1))
        # Aviso, no accion: con alimentacion externa pb11 DEBE valer 0. Si vale
        # 1, esa placa se creera en bateria y se callara. Se dice y se deja la
        # decision a una persona.
        if (pwr >> 3) & 1 and (pwr & 1) and (flags & F_BAT):
            dice("    ⚠️ pb11=1 con alimentacion externa: esta placa se creera en")
            dice("       bateria y dejara de transmitir. Revisa el pin antes de")
            dice("       instalarla, o apagale CFG_F_BAT a mano (HR 0 bit4).")
    except Exception as e:
        dice("    IR 12 no disponible (%s)" % e)
    if TANK is not None:
        txn(sock, struct.pack(">BHH", 6, 5, TANK))
        txn(sock, struct.pack(">BHH", 6, 9, 0xA5))
        dice("    tank_id = %d escrito y guardado" % TANK)
    v = leer(sock, 3, 0, 6)
    dice("    releido: HR 0=0x%04X  HR 5=%d" % (v[0], v[5]))


# ---------------------------------------------------------------- sondas
def puerto_switch():
    try:
        d = json.load(urllib.request.urlopen(
            "http://127.0.0.1:8080/api/switches", timeout=3))
        for s in d.get("switches", []):
            for p in s.get("puertos", []):
                for h in [p]:
                    pass
        # el puerto concreto se identifica por el que NO alimenta a un tanque
        # conocido; se devuelven todos los que entregan, que es lo comparable
        return {("%s/%s" % (s["clave"][:12], p["rotulo"])): p.get("ma")
                for s in d.get("switches", []) for p in s.get("puertos", [])
                if p.get("entregando")}
    except Exception:
        return None


def nuevas():
    try:
        d = json.load(urllib.request.urlopen(
            "http://127.0.0.1:8080/api/tanks", timeout=3))
        return {r["mac"]: r.get("count") for r in d.get("nuevas", [])}
    except Exception:
        return None


def dhcp_ultimo():
    try:
        s = subprocess.run(["journalctl", "-u", "dnsmasq", "--since", "-2 min",
                            "--no-pager", "-o", "cat"],
                           capture_output=True, text=True, timeout=5).stdout
        ls = [l for l in s.splitlines() if MAC in l.lower()]
        return ls[-1] if ls else None
    except Exception:
        return None


dice("vigilando %s (%s). Corta y repon su alimentacion SPE cuando quieras." % (MAC, IP))
dice("Ctrl-C para dejarlo.")
ant_pot, ant_dhcp, ant_nue, modbus_visto = None, None, None, False
while True:
    pot = puerto_switch()
    if pot is not None and pot != ant_pot:
        if ant_pot is not None:
            fue = set(ant_pot) - set(pot)
            vino = set(pot) - set(ant_pot)
            if fue:
                dice("POTENCIA: dejan de entregar %s" % ", ".join(sorted(fue)))
            if vino:
                dice("POTENCIA: empiezan a entregar %s" % ", ".join(sorted(vino)))
        ant_pot = pot

    d = dhcp_ultimo()
    if d and d != ant_dhcp:
        dice("DHCP: %s" % d.strip())
        ant_dhcp = d

    n = nuevas()
    if n is not None and n != ant_nue:
        if MAC in n:
            dice("TRAMAS: la pasarela ve %s (pulsos=%s)" % (MAC, n[MAC]))
        elif ant_nue and MAC in ant_nue:
            dice("TRAMAS: %s deja de verse" % MAC)
        ant_nue = n

    if not modbus_visto:
        try:
            sock = socket.create_connection((IP, 502), timeout=0.3)
            modbus_visto = True
            dice("MODBUS: %s responde -- ventana abierta, actuando" % IP)
            sock.settimeout(3)
            try:
                rescatar(sock)
                dice(">>> RESCATE COMPLETO. Sigo vigilando por si se vuelve a callar.")
            except Exception as e:
                dice("!! fallo a mitad del rescate: %s" % e)
                modbus_visto = False
            finally:
                sock.close()
        except OSError:
            pass
    time.sleep(0.4)
