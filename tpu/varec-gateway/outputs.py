#!/usr/bin/env python3
"""Salidas de la pasarela Varec: MQTT, Modbus TCP, Modbus RTU y HTTP/JSON.

Todas leen del estado vivo (RAM), nunca del disco: consultar el valor actual
de un tanque no debe tocar el SSD.

Cada salida se puede habilitar por separado en el config: si algo no se usa,
no se carga su dependencia.
"""
import json
import os
import struct
import sys
import threading
import time

# --------------------------------------------------------------------------
# Mapa de registros Modbus
# --------------------------------------------------------------------------
# Un bloque de 10 registros por tanque, indexado por tank_id:
#
#   base = (tank_id - 1) * 10
#     +0,+1  valor en unidad de ingenieria (float32, big-endian)
#     +2,+3  count crudo del encoder (int32)
#     +4     errores del encoder
#     +5     antiguedad de la ultima muestra, en segundos
#     +6     estado: bit0 = online
#     +7     uptime del sensor, en minutos
#     +8,+9  reservados
#
# Con 30 tanques son 300 registros; Modbus admite hasta 65536, asi que escala
# sin problema. Se eligio bloque-por-tanque (y no un unit_id por tanque)
# porque un SCADA puede leerlos todos de una pasada.
REGS_PER_TANK = 10


def tank_regs(rec: dict) -> list:
    """Convierte una muestra en sus 10 registros Modbus."""
    if rec is None:
        return [0] * REGS_PER_TANK
    v = struct.unpack(">HH", struct.pack(">f", float(rec.get("value") or 0.0)))
    cnt = int(rec.get("count") or 0) & 0xFFFFFFFF
    return [
        v[0], v[1],
        (cnt >> 16) & 0xFFFF, cnt & 0xFFFF,
        int(rec.get("errors") or 0) & 0xFFFF,
        min(int(rec.get("age_s") or 0), 0xFFFF),
        1 if rec.get("online") else 0,
        min(int((rec.get("uptime_s") or 0) // 60), 0xFFFF),
        0, 0,
    ]


# ============================================================ MQTT
class MqttOut:
    def __init__(self, cfg, live, stop):
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            print("MQTT pedido pero falta paho-mqtt:  pip install paho-mqtt",
                  file=sys.stderr)
            raise
        self.cfg, self.live, self.stop = cfg, live, stop
        self.prefix = cfg.get("topic_prefix", "varec")
        self.qos = cfg.get("qos", 1)
        self.retain = cfg.get("retain", True)
        # La clave nunca en el config si se puede evitar.
        pw = os.environ.get("VAREC_MQTT_PASS") or cfg.get("password") or ""
        self.c = mqtt.Client()
        if cfg.get("username"):
            self.c.username_pw_set(cfg["username"], pw)
        self.c.will_set(f"{self.prefix}/gateway/status", "offline",
                        qos=1, retain=True)
        self.c.connect_async(cfg.get("host", "localhost"), cfg.get("port", 1883), 60)
        self.c.loop_start()
        self.c.publish(f"{self.prefix}/gateway/status", "online", qos=1, retain=True)
        print(f"[mqtt] publicando en {self.prefix}/<tank_id>/...")

    def on_sample(self, rec):
        t = f"{self.prefix}/{rec['tank_id']}"
        self.c.publish(f"{t}/value", f"{rec['value']:.3f}", self.qos, self.retain)
        self.c.publish(f"{t}/raw", json.dumps({
            "tank_id": rec["tank_id"], "name": rec["name"], "ts": rec["ts"],
            "value": rec["value"], "unit": rec["unit"], "count": rec["count"],
            "edges": rec["edges"], "errors": rec["errors"],
            "uptime_s": rec["uptime_s"], "mac": rec["mac"],
        }), self.qos, self.retain)

    def close(self):
        self.c.publish(f"{self.prefix}/gateway/status", "offline", qos=1, retain=True)
        self.c.loop_stop()


# ============================================================ Modbus (TCP/RTU)
class ModbusOut:
    """Servidor Modbus. La pasarela es ESCLAVO: el SCADA/PLC la consulta.

    Se implementa el servidor a mano sobre sockets/serial en vez de arrastrar
    pymodbus: solo hay que atender FC 03/04 (leer registros) sobre un mapa que
    ya vive en RAM. Menos dependencias en un equipo de campo.
    """

    def __init__(self, cfg_tcp, cfg_rtu, live, stop):
        self.live, self.stop = live, stop
        if cfg_tcp and cfg_tcp.get("enabled"):
            threading.Thread(target=self._tcp, args=(cfg_tcp,), daemon=True,
                             name="mb-tcp").start()
        if cfg_rtu and cfg_rtu.get("enabled"):
            threading.Thread(target=self._rtu, args=(cfg_rtu,), daemon=True,
                             name="mb-rtu").start()

    def on_sample(self, rec):
        pass                                   # lee del estado vivo al vuelo

    def _read(self, start: int, count: int) -> list:
        snap = self.live.snapshot()
        out = []
        for i in range(start, start + count):
            tid, off = i // REGS_PER_TANK + 1, i % REGS_PER_TANK
            out.append(tank_regs(snap.get(tid))[off])
        return out

    # -------------------------------------------------- TCP
    def _tcp(self, cfg):
        import socket
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((cfg.get("bind", "0.0.0.0"), cfg.get("port", 502)))
        srv.listen(8)
        srv.settimeout(1.0)
        print(f"[modbus-tcp] escuchando en :{cfg.get('port', 502)}")
        while not self.stop.is_set():
            try:
                cli, addr = srv.accept()
            except socket.timeout:
                continue
            threading.Thread(target=self._tcp_client, args=(cli, cfg),
                             daemon=True).start()
        srv.close()

    def _tcp_client(self, cli, cfg):
        cli.settimeout(30)
        try:
            while not self.stop.is_set():
                h = cli.recv(7)
                if len(h) < 7:
                    break
                tid, pid, ln, uid = struct.unpack(">HHHB", h)
                body = cli.recv(ln - 1)
                if len(body) < 2:
                    break
                fc = body[0]
                if fc in (3, 4):
                    start, cnt = struct.unpack(">HH", body[1:5])
                    if cnt < 1 or cnt > 125:
                        pdu = bytes([fc | 0x80, 3])
                    else:
                        regs = self._read(start, cnt)
                        pdu = bytes([fc, cnt * 2]) + b"".join(
                            struct.pack(">H", r) for r in regs)
                else:
                    pdu = bytes([fc | 0x80, 1])       # funcion no soportada
                cli.sendall(struct.pack(">HHHB", tid, pid, len(pdu) + 1, uid) + pdu)
        except Exception:
            pass
        finally:
            cli.close()

    # -------------------------------------------------- RTU
    def _rtu(self, cfg):
        try:
            import serial
        except ImportError:
            print("Modbus RTU pedido pero falta pyserial:  sudo apt install python3-serial",
                  file=sys.stderr)
            return
        uid = cfg.get("unit_id", 1)
        par = {"N": serial.PARITY_NONE, "E": serial.PARITY_EVEN,
               "O": serial.PARITY_ODD}[cfg.get("parity", "E")]
        try:
            s = serial.Serial(cfg.get("device", "/dev/ttyAMA2"),
                              cfg.get("baud", 19200), parity=par, timeout=0.05)
        except Exception as e:
            print(f"[modbus-rtu] no puedo abrir el puerto: {e}", file=sys.stderr)
            return
        print(f"[modbus-rtu] esclavo {uid} en {cfg.get('device')} "
              f"{cfg.get('baud')} 8{cfg.get('parity')}1")
        buf = b""
        last = time.time()
        while not self.stop.is_set():
            d = s.read(256)
            if d:
                buf += d
                last = time.time()
                continue
            # Silencio de 3.5 caracteres = fin de trama (regla de Modbus RTU)
            if buf and time.time() - last > 0.005:
                r = self._rtu_frame(buf, uid)
                if r:
                    s.write(r)
                buf = b""
        s.close()

    @staticmethod
    def crc16(d: bytes) -> int:
        c = 0xFFFF
        for b in d:
            c ^= b
            for _ in range(8):
                c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
        return c

    def _rtu_frame(self, f: bytes, uid: int):
        if len(f) < 8 or f[0] != uid:
            return None
        if struct.unpack("<H", f[-2:])[0] != self.crc16(f[:-2]):
            return None                        # CRC malo: ignorar, no responder
        fc = f[1]
        if fc not in (3, 4):
            pdu = bytes([uid, fc | 0x80, 1])
        else:
            start, cnt = struct.unpack(">HH", f[2:6])
            if cnt < 1 or cnt > 125:
                pdu = bytes([uid, fc | 0x80, 3])
            else:
                regs = self._read(start, cnt)
                pdu = bytes([uid, fc, cnt * 2]) + b"".join(
                    struct.pack(">H", r) for r in regs)
        return pdu + struct.pack("<H", self.crc16(pdu))


# ============================================================ HTTP / JSON
class HttpOut:
    def __init__(self, cfg, live, store, stop):
        import http.server
        self.live, self.store, self.stop = live, store, stop
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def _send(self, obj, code=200):
                b = json.dumps(obj, indent=2, default=str).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                p = self.path.split("?")[0].rstrip("/")
                snap = outer.live.snapshot()
                if p in ("", "/api", "/api/tanks"):
                    self._send({"tanks": list(snap.values()),
                                "n": len(snap), "ts": int(time.time())})
                elif p.startswith("/api/tank/"):
                    parts = p.split("/")
                    try:
                        tid = int(parts[3])
                    except (IndexError, ValueError):
                        return self._send({"error": "tank_id invalido"}, 400)
                    if len(parts) > 4 and parts[4] == "history":
                        tbl = {"raw": "samples_raw", "1m": "samples_1m",
                               "1h": "samples_1h"}.get(
                            self.path.split("res=")[-1].split("&")[0], "samples_1m")
                        since = int(time.time()) - 86400
                        self._send({"tank_id": tid, "table": tbl,
                                    "rows": outer.store.history(tid, tbl, since)})
                    else:
                        self._send(snap.get(tid) or {"error": "sin datos"},
                                   200 if tid in snap else 404)
                else:
                    self._send({"rutas": ["/api/tanks", "/api/tank/<id>",
                                          "/api/tank/<id>/history?res=raw|1m|1h"]}, 404)

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(
            (cfg.get("bind", "0.0.0.0"), cfg.get("port", 8080)), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True,
                         name="http").start()
        print(f"[http] sirviendo en :{cfg.get('port', 8080)}")

    def on_sample(self, rec):
        pass


# ============================================================ fabrica
def build_outputs(cfg, live, store, stop) -> list:
    outs = []
    if cfg.get("mqtt", {}).get("enabled"):
        try:
            outs.append(MqttOut(cfg["mqtt"], live, stop))
        except Exception as e:
            print(f"[mqtt] deshabilitado: {e}", file=sys.stderr)
    if cfg.get("modbus_tcp", {}).get("enabled") or cfg.get("modbus_rtu", {}).get("enabled"):
        outs.append(ModbusOut(cfg.get("modbus_tcp"), cfg.get("modbus_rtu"), live, stop))
    if cfg.get("http", {}).get("enabled"):
        outs.append(HttpOut(cfg["http"], live, store, stop))
    return outs
