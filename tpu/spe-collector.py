#!/usr/bin/env python3
"""
Concentrador de datos de la TPU (CM5).

Recibe:
  - Tramas SPE de la ATT: Ethernet crudo, ethertype 0x88B5, payload "VARE"
    (magic, seq, count, edges, errors, uptime_ms). Llegan por el enlace
    10BASE-T1L a traves del power switch.
  - Lineas del RS-485 (/dev/ttyAMA2).

Sirve:
  - HTTP/JSON en :8080 para que un equipo conectado al RJ45 consulte los datos.
    La laptop NO toca la red SPE: solo habla con la CM5.

Todo con la biblioteca estandar; no requiere instalar nada.
"""
import http.server
import json
import os
import socket
import struct
import sys
import threading
import time
from collections import deque

SPE_IFACE = os.environ.get("SPE_IFACE", "")     # vacio = autodetectar por driver
RS485_DEV = os.environ.get("RS485_DEV", "/dev/ttyAMA2")
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))
ETHERTYPE = 0x88B5
MAGIC = 0x56415245                              # "VARE"
HIST = 500

_lock = threading.Lock()
_state = {
    "spe": {"last": None, "frames": 0, "bad": 0, "last_seen": None},
    "rs485": {"last": None, "lines": 0, "last_seen": None},
    "started": time.time(),
}
_hist_spe = deque(maxlen=HIST)
_hist_485 = deque(maxlen=HIST)


def find_spe_iface():
    """La interfaz SPE se identifica por DRIVER (adin1110), NUNCA por nombre:
    los nombres eth1/eth2/eth3 se intercambian entre arranques en esta CM5."""
    if SPE_IFACE:
        return SPE_IFACE
    base = "/sys/class/net"
    for name in sorted(os.listdir(base)):
        try:
            drv = os.path.basename(os.readlink(f"{base}/{name}/device/driver"))
            if drv == "adin1110":
                return name
        except OSError:
            continue
    return None


def spe_reader():
    iface = find_spe_iface()
    if not iface:
        print("[spe] no encuentro la interfaz del ADIN1110", file=sys.stderr)
        return
    print(f"[spe] escuchando ethertype 0x{ETHERTYPE:04X} en {iface}")
    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETHERTYPE))
    s.bind((iface, 0))
    while True:
        pkt = s.recv(2048)
        if len(pkt) < 14 + 24:
            continue
        src = ":".join(f"{b:02x}" for b in pkt[6:12])
        magic, seq, count, edges, errors, uptime = struct.unpack("!IIiIII", pkt[14:14 + 24])
        if magic != MAGIC:
            with _lock:
                _state["spe"]["bad"] += 1
            continue
        rec = {
            "src": src, "seq": seq, "count": count,
            "edges": edges, "errors": errors, "uptime_ms": uptime,
            "ts": time.time(),
        }
        with _lock:
            _state["spe"]["last"] = rec
            _state["spe"]["frames"] += 1
            _state["spe"]["last_seen"] = rec["ts"]
            _hist_spe.append(rec)


def rs485_reader():
    """Lectura cruda del tty; no requiere pyserial. El puerto lo deja a 115200 8N1
    el propio stty al arrancar el servicio (ver la unidad systemd)."""
    while True:
        try:
            with open(RS485_DEV, "rb", buffering=0) as f:
                print(f"[rs485] leyendo {RS485_DEV}")
                buf = b""
                while True:
                    b = f.read(1)
                    if not b:
                        time.sleep(0.05)
                        continue
                    if b in (b"\n", b"\r"):
                        if buf:
                            rec = {"line": buf.decode("utf-8", "replace"), "ts": time.time()}
                            with _lock:
                                _state["rs485"]["last"] = rec
                                _state["rs485"]["lines"] += 1
                                _state["rs485"]["last_seen"] = rec["ts"]
                                _hist_485.append(rec)
                            buf = b""
                    else:
                        buf += b
                        if len(buf) > 200:
                            buf = b""
        except Exception as e:
            print(f"[rs485] {e}; reintento en 3s", file=sys.stderr)
            time.sleep(3)


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        body = json.dumps(obj, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        with _lock:
            if path in ("", "/api", "/api/data"):
                now = time.time()
                spe = dict(_state["spe"])
                r485 = dict(_state["rs485"])
                spe["age_s"] = round(now - spe["last_seen"], 2) if spe["last_seen"] else None
                r485["age_s"] = round(now - r485["last_seen"], 2) if r485["last_seen"] else None
                self._send({
                    "spe": spe,
                    "rs485": r485,
                    "uptime_s": round(now - _state["started"], 1),
                })
            elif path == "/api/spe/history":
                self._send(list(_hist_spe))
            elif path == "/api/rs485/history":
                self._send(list(_hist_485))
            else:
                self._send({"error": "no existe",
                            "rutas": ["/api/data", "/api/spe/history", "/api/rs485/history"]}, 404)

    def log_message(self, *a):
        pass    # sin ruido en el journal


def main():
    threading.Thread(target=spe_reader, daemon=True).start()
    threading.Thread(target=rs485_reader, daemon=True).start()
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler)
    print(f"[http] sirviendo en 0.0.0.0:{HTTP_PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
