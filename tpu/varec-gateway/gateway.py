#!/usr/bin/env python3
"""
Pasarela Varec — corre en la CM5 de la TPU.

Recibe la medida de N sensores Varec 2500 (tarjeta ATT) por SPE, la almacena
en el SSD M.2 y la reexpone por MQTT, Modbus TCP, Modbus RTU y HTTP/JSON.

    ATT (encoder) --SPE 0x88B5--> power switch --> TPU  ==> MQTT
                                                        ==> Modbus TCP/RTU
                                                        ==> HTTP/JSON

Cada tanque se identifica por el `tank_id` que la propia ATT lleva en su
EEPROM y embebe en cada trama: la placa lleva su identidad consigo, asi que
se puede sustituir una ATT averiada sin tocar la pasarela.

Arranque:  python3 gateway.py --config /etc/varec-gateway/config.yaml
"""
import argparse
import os
import queue
import signal
import socket
import sqlite3
import struct
import sys
import threading
import time
from collections import defaultdict

# ---------------------------------------------------------------- constantes
ETHERTYPE = 0x88B5
MAGIC = 0x56415245                      # "VARE"

# Trama de la ATT, big-endian. Cada version ANADE campos al final, nunca
# reordena, asi que se puede parsear por version y aceptar varias a la vez.
# Con OTA la flota estara a medio actualizar: rechazar por version seria
# tirar datos buenos.
#   v1: magic ver tank seq count edges errors uptime
#   v2: + level_mm (calibrado en la ATT) tras count
#   v3: + temp_c10 + humi_rh10 al final
FRAME_FMT_BY_VER = {
    1: "!IHHIiIII",
    2: "!IHHIiiIII",
    3: "!IHHIiiIIIhh",
}
FRAME_LEN_BY_VER = {v: struct.calcsize(f) for v, f in FRAME_FMT_BY_VER.items()}
FRAME_MIN_LEN = min(FRAME_LEN_BY_VER.values())
ATT_NA = -32768          # centinela de la ATT: "sin sensor / sin dato"


def parse_att_frame(payload: bytes):
    """Decodifica el payload (sin cabecera Ethernet).

    Devuelve un dict, o el entero de la version si no la conocemos, o None
    si no es una trama nuestra.
    """
    if len(payload) < 6:
        return None
    magic, ver = struct.unpack("!IH", payload[:6])
    if magic != MAGIC:
        return None
    fmt = FRAME_FMT_BY_VER.get(ver)
    if fmt is None:
        return ver
    n = FRAME_LEN_BY_VER[ver]
    if len(payload) < n:
        return None
    f = struct.unpack(fmt, payload[:n])
    level = temp = humi = None
    if ver == 1:
        _, _, tank, seq, count, edges, errors, uptime = f
    elif ver == 2:
        _, _, tank, seq, count, level, edges, errors, uptime = f
    else:
        _, _, tank, seq, count, level, edges, errors, uptime, temp, humi = f
        # La ATT no lleva sensor de humedad: manda el centinela. Guardar
        # None y no 0.0, para no inventar una lectura que no existe.
        temp = None if temp == ATT_NA else temp / 10.0
        humi = None if humi == ATT_NA else humi / 10.0
    return {"ver": ver, "tank_id": tank, "seq": seq, "count": count,
            "level_mm": level, "edges": edges, "errors": errors,
            "uptime": uptime, "temp_c": temp, "humi_rh": humi}

STOP = threading.Event()


# ================================================================ utilidades
def now() -> int:
    return int(time.time())


def find_iface(driver: str, forced: str = "") -> str:
    """Localiza la interfaz SPE por DRIVER, nunca por nombre: los nombres
    eth1/eth2/eth3 se intercambian entre arranques en esta CM5."""
    if forced:
        return forced
    base = "/sys/class/net"
    for name in sorted(os.listdir(base)):
        try:
            drv = os.path.basename(os.readlink(f"{base}/{name}/device/driver"))
            if drv == driver:
                return name
        except OSError:
            continue
    raise RuntimeError(f"no encuentro ninguna interfaz con driver '{driver}'")


# ================================================================ almacen
class Store:
    """SQLite en el SSD. Un solo hilo escribe (cola) para no pelear por el
    lock; los lectores usan sus propias conexiones en modo WAL."""

    def __init__(self, path: str, retention: dict):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path
        self.retention = retention
        self.q: "queue.Queue" = queue.Queue(maxsize=10000)
        self._init_schema()
        threading.Thread(target=self._writer, daemon=True, name="store").start()
        threading.Thread(target=self._roller, daemon=True, name="roll").start()

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        return c

    def _init_schema(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "schema.sql")) as f:
            sql = f.read()
        c = self._conn()
        c.executescript(sql)
        # La base ya desplegada no tiene las columnas de ambiente: anadirlas
        # aqui. ALTER TABLE ... ADD COLUMN es barato y no toca los datos.
        for col, typ in (("temp_c", "REAL"), ("humi_rh", "REAL")):
            try:
                c.execute(f"ALTER TABLE samples_raw ADD COLUMN {col} {typ}")
            except Exception:
                pass   # ya existia
        c.commit()
        c.close()

    def put(self, rec: dict):
        try:
            self.q.put_nowait(rec)
        except queue.Full:
            # Perder una muestra es preferible a bloquear la ingesta: el
            # siguiente push llega en 30s. Se registra para no ocultarlo.
            print("[store] cola llena, muestra descartada", file=sys.stderr)

    def _writer(self):
        c = self._conn()
        pend = []
        last_flush = time.time()
        while not STOP.is_set():
            try:
                pend.append(self.q.get(timeout=1.0))
            except queue.Empty:
                pass
            # Agrupar escrituras: con 30+ tanques evita un commit por trama
            # y reduce el desgaste del SSD.
            if pend and (len(pend) >= 50 or time.time() - last_flush > 5):
                try:
                    c.executemany(
                        "INSERT OR REPLACE INTO samples_raw"
                        "(ts,tank_id,count,value,edges,errors,uptime_s,"
                        "temp_c,humi_rh)"
                        " VALUES(?,?,?,?,?,?,?,?,?)",
                        [(r["ts"], r["tank_id"], r["count"], r["value"],
                          r["edges"], r["errors"], r["uptime_s"],
                          r.get("temp_c"), r.get("humi_rh")) for r in pend],
                    )
                    c.commit()
                except Exception as e:
                    print(f"[store] error escribiendo: {e}", file=sys.stderr)
                pend.clear()
                last_flush = time.time()
        c.close()

    def _roller(self):
        """Consolida crudo -> 1m -> 1h y purga lo vencido. Sin esto, el
        historico largo no cabe."""
        while not STOP.is_set():
            for _ in range(600):                 # cada 10 min, saliendo rapido
                if STOP.is_set():
                    return
                time.sleep(1)
            try:
                c = self._conn()
                # crudo -> minuto
                c.execute("""
                    INSERT OR REPLACE INTO samples_1m
                      (ts,tank_id,value_avg,value_min,value_max,n,errors)
                    SELECT (ts/60)*60, tank_id, AVG(value), MIN(value), MAX(value),
                           COUNT(*), MAX(errors)
                      FROM samples_raw WHERE ts < strftime('%s','now') - 120
                     GROUP BY tank_id, ts/60
                """)
                # minuto -> hora
                c.execute("""
                    INSERT OR REPLACE INTO samples_1h
                      (ts,tank_id,value_avg,value_min,value_max,n,errors)
                    SELECT (ts/3600)*3600, tank_id, AVG(value_avg), MIN(value_min),
                           MAX(value_max), SUM(n), MAX(errors)
                      FROM samples_1m WHERE ts < strftime('%s','now') - 7200
                     GROUP BY tank_id, ts/3600
                """)
                # retencion
                r = self.retention
                c.execute("DELETE FROM samples_raw WHERE ts < ?",
                          (now() - r["raw_days"] * 86400,))
                c.execute("DELETE FROM samples_1m WHERE ts < ?",
                          (now() - r["minute_days"] * 86400,))
                c.execute("DELETE FROM samples_1h WHERE ts < ?",
                          (now() - r["hour_days"] * 86400,))
                c.commit()
                c.close()
            except Exception as e:
                print(f"[store] error consolidando: {e}", file=sys.stderr)

    def upsert_tank(self, tank_id: int, mac: str):
        c = self._conn()
        row = c.execute("SELECT mac FROM tanks WHERE tank_id=?", (tank_id,)).fetchone()
        if row is None:
            c.execute("INSERT INTO tanks(tank_id,mac,first_seen,last_seen) VALUES(?,?,?,?)",
                      (tank_id, mac, now(), now()))
            c.execute("INSERT INTO events(ts,tank_id,kind,detail) VALUES(?,?,?,?)",
                      (now(), tank_id, "online", f"alta automatica, mac={mac}"))
        else:
            if row[0] and row[0] != mac:
                # Dos placas con el mismo tank_id: error de campo. Gritarlo.
                c.execute("INSERT INTO id_conflicts(ts,tank_id,mac_old,mac_new)"
                          " VALUES(?,?,?,?)", (now(), tank_id, row[0], mac))
                c.execute("INSERT INTO events(ts,tank_id,kind,detail) VALUES(?,?,?,?)",
                          (now(), tank_id, "conflict",
                           f"tank_id duplicado: {row[0]} -> {mac}"))
                print(f"[!] CONFLICTO: tank_id {tank_id} visto en {row[0]} y {mac}",
                      file=sys.stderr)
            c.execute("UPDATE tanks SET mac=?, last_seen=? WHERE tank_id=?",
                      (mac, now(), tank_id))
        c.commit()
        c.close()

    def roster(self, live: dict) -> list:
        """Todos los tanques CONOCIDOS, no solo los que reportan ahora.

        Un sensor que deja de responder es informacion operativa y tiene
        que seguir en la lista, marcado como caido. Si solo se listara lo
        vivo, un reinicio de la pasarela borraria de la vista justo los
        sensores que hay que ir a revisar.
        """
        c = self._conn()
        out, seen = [], set()
        t_now = now()
        for tid, name, mac, unit, last_seen in c.execute(
                "SELECT tank_id,name,mac,unit,last_seen FROM tanks ORDER BY tank_id"):
            seen.add(tid)
            if tid in live:
                out.append(live[tid])
                continue
            # No reporta: reconstruir su ultima medida conocida del historico
            row = c.execute(
                "SELECT ts,count,value,errors,temp_c,humi_rh FROM samples_raw"
                " WHERE tank_id=? ORDER BY ts DESC LIMIT 1", (tid,)).fetchone()
            ts = (row[0] if row else last_seen) or 0
            out.append({
                "tank_id": tid, "name": name or f"tank{tid}", "mac": mac,
                "unit": unit or "mm",
                "count": row[1] if row else None,
                "value": row[2] if row else None,
                "errors": row[3] if row else None,
                "temp_c": row[4] if row else None,
                "humi_rh": row[5] if row else None,
                "ts": ts, "age_s": (t_now - ts) if ts else None,
                "online": False,
            })
        # Vivos que aun no estan en la tabla (alta en curso)
        for tid, r in live.items():
            if tid not in seen:
                out.append(r)
        c.close()
        out.sort(key=lambda t: t["tank_id"])
        return out

    def tank_cfg(self) -> dict:
        c = self._conn()
        out = {r[0]: {"name": r[1], "scale": r[2], "offset": r[3], "unit": r[4]}
               for r in c.execute("SELECT tank_id,name,scale,offset,unit FROM tanks")}
        c.close()
        return out

    def history(self, tank_id: int, table: str, since: int, limit: int = 5000):
        c = self._conn()
        rows = c.execute(
            f"SELECT * FROM {table} WHERE tank_id=? AND ts>=? ORDER BY ts DESC LIMIT ?",
            (tank_id, since, limit)).fetchall()
        c.close()
        return rows


# ================================================================ estado vivo
class Live:
    """Ultimo valor de cada tanque, en RAM. Es lo que consultan MQTT, Modbus
    y HTTP: nadie toca el disco para leer el valor actual."""

    def __init__(self, offline_after: int):
        self.lock = threading.Lock()
        self.tanks: dict = {}
        self.offline_after = offline_after

    def update(self, rec: dict):
        with self.lock:
            self.tanks[rec["tank_id"]] = rec

    def snapshot(self) -> dict:
        t = now()
        with self.lock:
            out = {}
            for tid, r in self.tanks.items():
                d = dict(r)
                d["age_s"] = t - r["ts"]
                d["online"] = d["age_s"] <= self.offline_after
                out[tid] = d
            return out


# ================================================================ ingesta
_warned_ver: set = set()
_warned_id: set = set()


def ingest(cfg: dict, store: Store, live: Live, outs: list):
    iface = find_iface(cfg["ingest"]["iface_driver"], cfg["ingest"].get("iface", ""))
    print(f"[spe] escuchando ethertype 0x{ETHERTYPE:04X} en {iface}")
    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETHERTYPE))
    s.bind((iface, 0))
    s.settimeout(1.0)
    tcfg = store.tank_cfg()
    last_cfg = time.time()

    while not STOP.is_set():
        try:
            pkt = s.recv(2048)
        except socket.timeout:
            continue
        except OSError as e:
            print(f"[spe] {e}", file=sys.stderr)
            time.sleep(1)
            continue

        if len(pkt) < 14 + FRAME_MIN_LEN:
            continue
        mac = ":".join(f"{b:02x}" for b in pkt[6:12])
        fr = parse_att_frame(pkt[14:])
        if fr is None:
            continue
        if isinstance(fr, int):
            ver = fr
            # Una ATT con firmware distinto: mejor decirlo que interpretar mal
            # sus bytes. Se avisa una vez por MAC, no en cada trama.
            if mac not in _warned_ver:
                _warned_ver.add(mac)
                print(f"[spe] {mac} habla version {ver}, no soportada"
                      f" -- ignorada", file=sys.stderr)
            continue
        ver, tank_id, seq = fr["ver"], fr["tank_id"], fr["seq"]
        count, edges, errors = fr["count"], fr["edges"], fr["errors"]
        uptime = fr["uptime"]
        if tank_id == 0:
            # 0 = sin asignar. Es un error de puesta en marcha, no un dato.
            if mac not in _warned_id:
                _warned_id.add(mac)
                print(f"[spe] {mac} reporta tank_id=0 (sin asignar): escribele el"
                      f" HR 5 por Modbus y guarda con HR 9=0xA5", file=sys.stderr)
            continue

        if time.time() - last_cfg > 30:
            tcfg = store.tank_cfg()
            last_cfg = time.time()
        if tank_id not in tcfg:
            store.upsert_tank(tank_id, mac)
            tcfg = store.tank_cfg()
        else:
            store.upsert_tank(tank_id, mac)

        c = tcfg.get(tank_id, {})
        rec = {
            "ts": now(), "tank_id": tank_id, "mac": mac, "seq": seq,
            "count": count, "edges": edges, "errors": errors,
            # La ATT manda MILISEGUNDOS (k_uptime_get_32). Convertir aqui: un
            # uptime de "121742 s" tras un reinicio delata el error de unidades.
            "uptime_s": uptime // 1000,
            "value": count * (c.get("scale") or 1.0) + (c.get("offset") or 0.0),
            "unit": c.get("unit") or "mm",
            "name": c.get("name") or f"tank{tank_id}",
            # Ambiente (v3). None = la placa no lo reporta.
            "temp_c": fr["temp_c"],
            "humi_rh": fr["humi_rh"],
            # Nivel que calcula la propia ATT (v2+). La calibracion BUENA es
            # la de la TPU (scale/offset); esto queda como referencia.
            "att_level_mm": fr["level_mm"],
        }
        live.update(rec)
        store.put(rec)
        for o in outs:
            try:
                o.on_sample(rec)
            except Exception as e:
                print(f"[out] {e}", file=sys.stderr)
    s.close()


# ============================================ ingesta por Modbus RTU
# El SPE del ATT (ADIN2111/OA-SPI) esta roto, asi que el ATT solo habla por
# RS-485. Aqui la gateway hace de MAESTRO Modbus: consulta al ATT (esclavo) y
# mete el mismo `rec` en el pipeline (Store/Live/outputs), igual que la ingesta
# SPE. Usa el nivel ya calibrado por el ATT (IR7-8) como valor.
def _rtu_crc16(d: bytes) -> int:
    c = 0xFFFF
    for b in d:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
    return c


def _rtu_read(ser, unit: int, func: int, addr: int, count: int):
    req = struct.pack(">BBHH", unit, func, addr, count)
    req += struct.pack("<H", _rtu_crc16(req))
    ser.reset_input_buffer()
    ser.write(req)
    hdr = ser.read(3)                       # unit, func, byte_count
    if len(hdr) < 3 or hdr[0] != unit or hdr[1] != func:
        return None
    nbytes = hdr[2]
    body = ser.read(nbytes + 2)             # datos + CRC
    if len(body) < nbytes + 2:
        return None
    data = body[:nbytes]
    if struct.unpack("<H", body[nbytes:nbytes + 2])[0] != _rtu_crc16(hdr + data):
        return None
    return struct.unpack(">" + "H" * (nbytes // 2), data)


def _s32(hi: int, lo: int) -> int:
    v = ((hi & 0xFFFF) << 16) | (lo & 0xFFFF)
    return v - (1 << 32) if v & 0x80000000 else v


def ingest_modbus(cfg: dict, store: Store, live: Live, outs: list):
    mc = cfg.get("modbus_ingest", {})
    import serial
    par = {"N": serial.PARITY_NONE, "E": serial.PARITY_EVEN,
           "O": serial.PARITY_ODD}[mc.get("parity", "E")]
    dev = mc.get("device", "/dev/ttyAMA2")
    unit = mc.get("unit_id", 1)
    poll = mc.get("poll_s", 2)
    fallback_id = mc.get("tank_id", 0)      # si el ATT reporta tank_id=0
    try:
        ser = serial.Serial(dev, mc.get("baud", 19200), parity=par,
                            stopbits=1, bytesize=8, timeout=0.4)
    except Exception as e:
        print(f"[rtu] no pude abrir {dev}: {e}", file=sys.stderr)
        return
    print(f"[rtu] ingesta Modbus del ATT en {dev} unit {unit} cada {poll}s")
    while not STOP.is_set():
        ir = _rtu_read(ser, unit, 4, 0, 10)     # IR 0-9
        hr = _rtu_read(ser, unit, 3, 5, 1)      # HR 5 = tank_id
        if ir is None:
            time.sleep(poll)
            continue
        count    = _s32(ir[0], ir[1])
        edges    = ((ir[2] & 0xFFFF) << 16) | (ir[3] & 0xFFFF)
        errors   = ir[4]
        uptime_s = ir[5]                        # IR5 ya viene en segundos
        level    = _s32(ir[7], ir[8])           # nivel mm (calibrado por el ATT)
        cal      = ir[9]
        tank_id  = (hr[0] if hr else 0) or fallback_id
        if tank_id == 0:
            time.sleep(poll)
            continue                            # sin asignar: no es un dato

        store.upsert_tank(tank_id, dev)
        c = store.tank_cfg().get(tank_id, {})
        base = level if cal else count          # calibrado -> nivel; si no, cuenta
        rec = {
            "ts": now(), "tank_id": tank_id, "mac": dev, "seq": 0,
            "count": count, "edges": edges, "errors": errors,
            "uptime_s": uptime_s,
            "value": base * (c.get("scale") or 1.0) + (c.get("offset") or 0.0),
            "unit": c.get("unit") or "mm",
            "name": c.get("name") or f"tank{tank_id}",
        }
        live.update(rec)
        store.put(rec)
        for o in outs:
            try:
                o.on_sample(rec)
            except Exception as e:
                print(f"[out] {e}", file=sys.stderr)
        time.sleep(poll)
    ser.close()


# ================================================================ main
def load_cfg(path: str) -> dict:
    try:
        import yaml
    except ImportError:
        print("Falta PyYAML:  sudo apt install python3-yaml", file=sys.stderr)
        sys.exit(1)
    with open(path) as f:
        return yaml.safe_load(f)


def main():
    ap = argparse.ArgumentParser(description="Pasarela Varec (SPE -> MQTT/Modbus/HTTP)")
    ap.add_argument("--config", default="/etc/varec-gateway/config.yaml")
    args = ap.parse_args()
    cfg = load_cfg(args.config)
    cfg["_path"] = args.config          # la UI necesita saber donde guardar

    def stop(*_):
        print("\n[gw] parando...")
        STOP.set()
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    store = Store(cfg["store"]["path"], cfg["store"]["retention"])
    live = Live(cfg["ingest"]["offline_after_s"])

    outs = []
    from outputs import build_outputs           # noqa: E402
    outs = build_outputs(cfg, live, store, STOP)

    # Ingesta por dos caminos: SPE (tramas L2) y/o Modbus RTU (RS-485).
    # Con el SPE del ATT roto, el RTU es el que trae los datos.
    if cfg.get("modbus_ingest", {}).get("enabled"):
        threading.Thread(target=ingest_modbus, args=(cfg, store, live, outs),
                         daemon=True, name="rtu").start()
    if cfg["ingest"].get("enabled", True):
        threading.Thread(target=ingest, args=(cfg, store, live, outs),
                         daemon=True, name="spe").start()

    while not STOP.is_set():
        time.sleep(0.5)
    print("[gw] fin")


if __name__ == "__main__":
    main()
