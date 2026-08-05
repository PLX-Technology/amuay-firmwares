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

# --- Telemetria del power switch (MPS): corriente por puerto del LTC4296 ---
# Ethertype propio para no mezclarla con la de los tanques.
MPS_ETHERTYPE = 0x88B6
MPS_MAGIC     = 0x4D505331          # "MPS1"
MPS_I_NA      = -32768              # el puerto no da lectura valida
#   magic ver nports seq uptime_ms  iout[4]  pxst[4]
MPS_FMT       = "!IHHII" + "h" * 4 + "H" * 4
MPS_LEN       = struct.calcsize(MPS_FMT)
#   v2, al final de la trama: gcmd unlocks vin_mv vin_ok disc_n
MPS_FMT_V2    = "!HHiHH"
MPS_LEN_V2    = struct.calcsize(MPS_FMT_V2)
#   v3, DESPUES del bloque v2: dev_id (identidad estable del USN)
MPS_FMT_V3    = "!Q"
MPS_LEN_V3    = struct.calcsize(MPS_FMT_V3)
# Llave de desbloqueo del LTC4296 (GCMD). Con el chip bloqueado, las
# escrituras se ignoran EN SILENCIO: no hay error que mirar.
LTC_UNLOCK_KEY = 0x05
# Estado PSE (bits 2:0 de PxST). El 2 es el unico que significa "entregando".
PSE_ESTADO = {0: "deshabilitado", 1: "durmiendo", 2: "entregando",
              3: "buscando", 4: "error", 5: "inactivo", 6: "pre-deteccion",
              7: "desconocido"}

# --- Telemetria del field switch (MFS) -------------------------------------
# Mismo ethertype que el MPS: se distinguen por el magic. Un field switch tiene
# 5 puertos PSE en su LTC4296 (el MPS tiene 4).
MFS_MAGIC     = 0x4D465331          # "MFS1"
MFS_PORTS     = 5
#   magic ver nports seq uptime_ms dev_id   iout[5]  pxst[5]
MFS_FMT       = "!IHHIIQ" + "h" * MFS_PORTS + "H" * MFS_PORTS
MFS_LEN       = struct.calcsize(MFS_FMT)
# Bloque de chip identico al v2 del MPS: gcmd unlocks vin_mv vin_ok disc_n
MFS_FMT_CHIP  = MPS_FMT_V2
MFS_LEN_CHIP  = MPS_LEN_V2
#   v2, DESPUES del bloque de chip: GFLTEV (fallos globales del LTC4296)
MFS_FMT_V2    = "!H"
MFS_LEN_V2    = struct.calcsize(MFS_FMT_V2)
# BIT(0) de GFLTEV. Un puerto con esto enclavado NO vuelve a clasificar
# por mucho que se reintente, y desde fuera solo se ve "deshabilitado".
LTC_LOW_CKT_BRK = 0x0001
#   v3, DESPUES de gfltev: VECINOS. Quien se ve por cada puerto del switch.
#   vec_total(H) vec_idx0(H) vec_n(B) pad(B) + 8 x { mac[6] port pad }
MFS_VEC_N     = 8
MFS_FMT_V3    = "!HHBB"
MFS_LEN_V3    = struct.calcsize(MFS_FMT_V3) + MFS_VEC_N * 8

# ⚠️ POR QUE LA IDENTIDAD VA DENTRO DE LA TRAMA Y NO ES LA MAC.
#
# La MAC del field switch se SORTEA EN CADA ARRANQUE
# (`field-switch/src/main.c`: srand(k_cycle_get_32()); mac_addr[3..5]=rand()).
# Indexar por MAC -- que es lo que hace el MPS hoy -- crearia un equipo nuevo
# en el panel cada vez que un switch se reinicia. Con 50 en campo, el panel se
# llenaria de fantasmas en dias.
#
# El MPS tampoco se salva por otro motivo: su MAC esta CABLEADA en el firmware
# (02:00:00:AD:4C:01), asi que dos power switches colisionarian entre si.
#
# Por eso `dev_id` es un entero de 64 bits derivado del USN del MAX32690
# (MXC_SYS_GetUSN), que es unico por chip y sobrevive a reinicios y a
# regrabados. La MAC se sigue guardando, pero solo como dato informativo.
DEV_SIN_ID = 0                      # firmware antiguo: no manda dev_id

# --- ⚠️ SERIGRAFIA DEL FIELD SWITCH: los numeros NO coinciden --------------
# De `field-switch/MAPA-SLOTS.md`, leido del esquematico:
#
#   Rotulo   datos (macPort)   potencia (LTC port)
#   Port 1        2            (uplink: recibe PoDL, no es PSE)
#   Port 2        1                  0
#   Port 3        0                  1
#   Port 4        5                  2
#   Port 5        4                  3
#   Port 6        3                  4
#
# El panel enseñaba el indice del LTC como si fuera el rotulo, con lo que
# habia un desplazamiento: la ATT conectada al "Port 4" aparecia como slot 3.
# Aqui se traduce TODO al rotulo, que es lo que el operario lee en la placa, y
# ademas permite juntar potencia (LTC) y vecinos (macPort) en la misma fila.
MFS_ROTULO_LTC = {0: "Port 2", 1: "Port 3", 2: "Port 4", 3: "Port 5", 4: "Port 6"}
MFS_ROTULO_MAC = {2: "Port 1", 1: "Port 2", 0: "Port 3", 5: "Port 4",
                  4: "Port 5", 3: "Port 6"}


def puerto_de_portmap(pm: int):
    """`portMap` de la tabla dinamica es una MASCARA DE BITS, no un indice.

    Bit N = macPort N. Se vio en hardware (2026-08-05): llegaban puertos 16 y
    32, imposibles en un ADIN6310 de seis puertos. Eran los bits 4 y 5. Al
    decodificarlos, cada vecino cayo en un puerto que si estaba entregando
    potencia, que es la comprobacion que lo confirma.

    Con VARIOS bits activos la direccion se aprendio por mas de un puerto y no
    se puede atribuir a ninguno: se descarta antes que colgarla del equivocado
    y torcer el arbol.
    """
    if pm <= 0 or (pm & (pm - 1)):
        return None
    return pm.bit_length() - 1


def parse_mfs_frame(payload: bytes):
    """Decodifica la telemetria de un field switch (sin cabecera Ethernet)."""
    if len(payload) < MFS_LEN:
        return None
    f = struct.unpack(MFS_FMT, payload[:MFS_LEN])
    if f[0] != MFS_MAGIC:
        return None
    ver, nports, seq, uptime_ms, dev_id = f[1], f[2], f[3], f[4], f[5]
    base = 6
    iout = f[base:base + MFS_PORTS]
    pxst = f[base + MFS_PORTS:base + 2 * MFS_PORTS]
    puertos = []
    for i in range(min(nports, MFS_PORTS)):
        st = pxst[i] & 0x7
        puertos.append({
            "puerto": i,
            # ⚠️ La serigrafia del MFS empieza en 1 igual que la del MPS, pero
            # el mapeo NO es trivial: `MAPA-SLOTS.md` documenta que macPort5 es
            # el slot rotulado "Port 4". Aqui se expone el indice del LTC4296
            # tal cual y la correspondencia se resuelve en la UI, para no
            # enterrar un mapeo discutible dentro del parser.
            "slot": i + 1,
            "rotulo": MFS_ROTULO_LTC.get(i, "LTC %d" % i),
            "ma": None if iout[i] == MPS_I_NA else iout[i],
            "estado": PSE_ESTADO.get(st, "?"),
            "entregando": st == 2,
            "pxst": pxst[i],
        })
    d = {"tipo": "mfs", "ver": ver, "seq": seq, "uptime_s": uptime_ms // 1000,
         "dev_id": dev_id, "puertos": puertos, "chip": None}
    if len(payload) >= MFS_LEN + MFS_LEN_CHIP:
        gcmd, unlocks, vin_mv, vin_ok, disc_n = struct.unpack(
            MFS_FMT_CHIP, payload[MFS_LEN:MFS_LEN + MFS_LEN_CHIP])
        d["chip"] = {
            "gcmd": gcmd,
            "bloqueado": (gcmd & LTC_UNLOCK_KEY) != LTC_UNLOCK_KEY,
            "redesbloqueos": unlocks,
            "vin_mv": None if vin_mv < 0 else vin_mv,
            "vin_en_rango": bool(vin_ok),
            "clasif_abandonadas": disc_n,
        }
        off = MFS_LEN + MFS_LEN_CHIP
        if ver >= 2 and len(payload) >= off + MFS_LEN_V2:
            (gfltev,) = struct.unpack(MFS_FMT_V2, payload[off:off + MFS_LEN_V2])
            d["chip"]["gfltev"] = gfltev
            # Explicito porque es la diferencia entre "no hay nada conectado" y
            # "el puerto esta bloqueado y no se va a recuperar solo".
            d["chip"]["interruptor_baja"] = bool(gfltev & LTC_LOW_CKT_BRK)
            off += MFS_LEN_V2
            if ver >= 3 and len(payload) >= off + MFS_LEN_V3:
                tot, idx0, n, _ = struct.unpack(
                    MFS_FMT_V3, payload[off:off + struct.calcsize(MFS_FMT_V3)])
                b = off + struct.calcsize(MFS_FMT_V3)
                vec = []
                for i in range(min(n, MFS_VEC_N)):
                    e = payload[b + i * 8:b + i * 8 + 8]
                    p = puerto_de_portmap(e[6])
                    if p is None:
                        continue
                    vec.append({"mac": ":".join("%02x" % x for x in e[:6]),
                                "puerto": p, "portmap": e[6]})
                # ⚠️ Es un TROZO de la tabla, no la tabla entera: el switch la
                # recorre en tramas sucesivas. Quien lo consuma debe ACUMULAR.
                d["vecinos"] = {"total": tot, "idx0": idx0, "trozo": vec}
    return d


def parse_mps_frame(payload: bytes):
    """Decodifica la telemetria del MPS (sin cabecera Ethernet)."""
    if len(payload) < MPS_LEN:
        return None
    f = struct.unpack(MPS_FMT, payload[:MPS_LEN])
    if f[0] != MPS_MAGIC:
        return None
    ver, nports, seq, uptime_ms = f[1], f[2], f[3], f[4]
    iout, pxst = f[5:9], f[9:13]
    puertos = []
    for i in range(min(nports, 4)):
        st = pxst[i] & 0x7
        puertos.append({
            "puerto": i,
            "slot": i + 1,                  # serigrafia del MPS: slot = puerto + 1
            "rotulo": "Slot %d" % (i + 1),
            # None = sin lectura valida. NO es cero.
            "ma": None if iout[i] == MPS_I_NA else iout[i],
            "estado": PSE_ESTADO.get(st, "?"),
            "entregando": st == 2,
            "pxst": pxst[i],
        })
    d = {"tipo": "mps", "ver": ver, "seq": seq, "uptime_s": uptime_ms // 1000,
         "puertos": puertos, "chip": None, "dev_id": DEV_SIN_ID}
    # Solo si la version lo anuncia Y los bytes estan: asi un MPS con firmware
    # v1 sigue funcionando contra esta pasarela.
    if ver >= 2 and len(payload) >= MPS_LEN + MPS_LEN_V2:
        gcmd, unlocks, vin_mv, vin_ok, disc_n = struct.unpack(
            MPS_FMT_V2, payload[MPS_LEN:MPS_LEN + MPS_LEN_V2])
        d["chip"] = {
            "gcmd": gcmd,
            "bloqueado": (gcmd & LTC_UNLOCK_KEY) != LTC_UNLOCK_KEY,
            "redesbloqueos": unlocks,
            # -1 = aun no ha habido ninguna clasificacion que medir. None, no 0:
            # un cero aqui se leeria como "el rail esta muerto".
            "vin_mv": None if vin_mv < 0 else vin_mv,
            "vin_en_rango": bool(vin_ok),
            "clasif_abandonadas": disc_n,
        }
    # v3: identidad estable, DESPUES del bloque v2. Mismo criterio que arriba:
    # solo si la version lo anuncia Y los bytes estan, para que un MPS con
    # firmware v1 o v2 siga funcionando contra esta pasarela sin tocar nada.
    off = MPS_LEN + MPS_LEN_V2
    if ver >= 3 and len(payload) >= off + MPS_LEN_V3:
        (d["dev_id"],) = struct.unpack(MPS_FMT_V3, payload[off:off + MPS_LEN_V3])
    return d


# Ultima telemetria del MPS. En memoria a proposito: es un panel de estado
# instantaneo, no una serie historica.
PSE_LIVE = {"ts": 0, "mac": None, "puertos": [], "chip": None}
PSE_LOCK = threading.Lock()

# --- Registro de switches (power switch + field switches encadenados) -------
# Clave: identidad ESTABLE del equipo. Ver el comentario de DEV_SIN_ID sobre
# por que no puede ser la MAC.
#
# En memoria, como PSE_LIVE: es un panel instantaneo. Si algun dia hace falta
# historico de consumo por slot, va a SQLite, no aqui.
SWITCHES = {}                       # clave -> dict del equipo
SW_LOCK = threading.Lock()
# Un equipo se marca caido pasado esto, pero NO se borra: que un field switch
# desaparezca del panel es justo lo que no queremos ver en una planta.
SW_VIVO_S = 15
# Se olvida solo tras un dia sin dar senales, para que una placa retirada
# acabe desapareciendo sin intervencion.
SW_OLVIDO_S = 86400


# --- Jerarquia: quien cuelga de quien ---------------------------------------
# Cada field switch reporta, a trozos, la tabla de MACs aprendidas de su switch
# y por que puerto ve cada una. Aqui se acumulan y se resuelve el arbol.
#
# ⚠️ SE ACUMULA CON CADUCIDAD, no se reemplaza: cada trama trae solo 8 entradas
# y la tabla se recorre en varias. Reemplazar dejaria el arbol parpadeando.
VEC = {}            # clave de equipo -> { mac: {"puerto": n, "ts": t} }
VEC_OLVIDO_S = 300  # una MAC no vista en 5 min se descarta


def vecinos_acumular(clave: str, trozo: list):
    t = now()
    d = VEC.setdefault(clave, {})
    for e in trozo:
        d[e["mac"]] = {"puerto": e["puerto"], "ts": t}
    for m in [m for m, v in d.items() if t - v["ts"] > VEC_OLVIDO_S]:
        del d[m]


def jerarquia(equipos: list, macs_tanque: dict) -> list:
    """Anota en cada equipo quien cuelga de sus puertos.

    `macs_tanque`: mac -> tank_id, para poder nombrar las ATT.

    ⚠️ Se resuelve por MAC porque es lo unico que el switch aprende. La
    identidad estable (dev_id) la aporta la pasarela, que ve la MAC de origen
    de cada trama de telemetria. Sin esa traduccion, un arbol construido sobre
    MACs que se sortean en cada arranque no valdria para nada.
    """
    por_mac = {e.get("mac"): e for e in equipos if e.get("mac")}
    raiz_macs = {e.get("mac") for e in equipos if e["tipo"] == "mps"}

    for e in equipos:
        vistos = VEC.get(e["clave"], {})

        # ⚠️ EL PUERTO DE SUBIDA VE TODO LO QUE HAY AGUAS ARRIBA.
        #
        # La tabla de direcciones del switch NO distingue arriba de abajo: por
        # el puerto por el que sube, este equipo ve el power switch, la TPU y
        # todos los tanques de las demas ramas. Tomar eso por "hijos" invierte
        # el arbol -- un field switch de banco llego a declararse padre del
        # power switch (2026-08-05).
        #
        # El puerto de subida es aquel por el que se ve la RAIZ (el power
        # switch). Si no se ve ninguna, se usa el puerto con mas direcciones:
        # el de subida agrega todo lo de arriba, asi que casi siempre gana.
        subida = None
        for mac, v in vistos.items():
            if mac in raiz_macs:
                subida = v["puerto"]
                break
        if subida is None and vistos:
            cuenta = {}
            for v in vistos.values():
                cuenta[v["puerto"]] = cuenta.get(v["puerto"], 0) + 1
            if cuenta:
                mx = max(cuenta.values())
                # Solo si destaca de verdad; con empate no se adivina.
                cands = [p for p, c in cuenta.items() if c == mx]
                if len(cands) == 1 and mx > 1:
                    subida = cands[0]
        e["puerto_subida"] = subida

        hijos = {}
        for mac, v in vistos.items():
            if subida is not None and v["puerto"] == subida:
                continue        # aguas arriba: no es hijo
            otro = por_mac.get(mac)
            if otro is not None and otro is not e:
                q = ("switch", otro["clave"], otro["tipo"])
            elif mac in macs_tanque:
                q = ("tanque", macs_tanque[mac], None)
            else:
                continue
            rot = (MFS_ROTULO_MAC.get(v["puerto"], "macPort %d" % v["puerto"])
                   if e["tipo"] == "mfs" else "slot %d" % (v["puerto"] + 1))
            hijos.setdefault(rot, []).append(
                {"tipo": q[0], "id": q[1], "clase": q[2], "mac": mac})
        e["hijos_por_puerto"] = dict(sorted(hijos.items()))
        e["n_vecinos"] = len(vistos)
    # Padre = el equipo que ve a este por alguno de sus puertos.
    for e in equipos:
        e["padre"] = None
        for otro in equipos:
            if otro is e:
                continue
            for lst in otro.get("hijos_por_puerto", {}).values():
                if any(h["tipo"] == "switch" and h["id"] == e["clave"] for h in lst):
                    e["padre"] = otro["clave"]
    return equipos


def sw_clave(tipo: str, dev_id: int, mac: str) -> str:
    """Identidad del equipo en el registro.

    Con `dev_id` (firmware nuevo) la identidad es estable de por vida. Sin el,
    se cae a la MAC y se marca `id_estable=False` para que el panel pueda
    avisar: en un field switch antiguo esa MAC cambia en cada arranque.
    """
    if dev_id and dev_id != DEV_SIN_ID:
        return f"{tipo}:{dev_id:016x}"
    return f"{tipo}:mac:{mac}"


def registrar_switch(tipo: str, mac: str, fr: dict):
    """Guarda/actualiza la telemetria de un switch en el registro."""
    dev_id = fr.get("dev_id", DEV_SIN_ID)
    clave = sw_clave(tipo, dev_id, mac)
    with SW_LOCK:
        eq = SWITCHES.get(clave)
        if eq is None:
            eq = SWITCHES[clave] = {"clave": clave, "tipo": tipo,
                                    "visto_primero": now()}
        eq["ts"] = now()
        eq["mac"] = mac
        eq["dev_id"] = dev_id
        eq["id_estable"] = bool(dev_id and dev_id != DEV_SIN_ID)
        eq["puertos"] = fr["puertos"]
        eq["chip"] = fr.get("chip")
        eq["uptime_s"] = fr["uptime_s"]
        eq["seq"] = fr["seq"]
        eq["ver"] = fr.get("ver")
    v = fr.get("vecinos")
    if v:
        vecinos_acumular(clave, v.get("trozo") or [])


def _potencia_por_slot(eq: dict):
    """Rellena `w` por slot y `w_total`. Comparte criterio con pse_snapshot():
    sin corriente o sin tension el resultado es None, NUNCA 0.0."""
    vin_mv = (eq.get("chip") or {}).get("vin_mv")
    total = None
    for p in eq["puertos"]:
        ma = p.get("ma")
        if vin_mv is None or ma is None:
            p["w"] = None
        else:
            p["w"] = round(vin_mv * ma / 1e6, 3)
            total = (total or 0.0) + p["w"]
    eq["w_total"] = None if total is None else round(total, 3)
    eq["w_vin_mv"] = vin_mv


def switches_snapshot(macs_tanque: dict = None) -> list:
    """Lista de switches conocidos, ordenada, con potencia y jerarquia.

    `macs_tanque` (mac -> tank_id) permite nombrar las ATT en el arbol. Se pasa
    desde la capa HTTP, que es la que tiene el estado vivo de los tanques.
    """
    t = now()
    with SW_LOCK:
        for clave in [k for k, v in SWITCHES.items()
                      if t - v.get("ts", 0) > SW_OLVIDO_S]:
            del SWITCHES[clave]
        equipos = []
        for eq in SWITCHES.values():
            c = dict(eq)
            c["puertos"] = [dict(p) for p in eq.get("puertos", [])]
            equipos.append(c)
    for eq in equipos:
        eq["edad_s"] = t - eq["ts"] if eq.get("ts") else None
        eq["vivo"] = eq["edad_s"] is not None and eq["edad_s"] <= SW_VIVO_S
        _potencia_por_slot(eq)
    # El power switch primero: es la raiz de la cadena de alimentacion.
    equipos.sort(key=lambda e: (e["tipo"] != "mps", e.get("dev_id") or 0,
                                e.get("mac") or ""))
    return jerarquia(equipos, macs_tanque or {})


def ingest_pse(cfg: dict):
    """Hilo propio para el ethertype del MPS. No toca la ingesta de tanques."""
    iface = find_iface(cfg["ingest"]["iface_driver"], cfg["ingest"].get("iface", ""))
    print(f"[pse] escuchando ethertype 0x{MPS_ETHERTYPE:04X} en {iface}")
    try:
        s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW,
                          socket.htons(MPS_ETHERTYPE))
        s.bind((iface, 0))
        s.settimeout(1.0)
    except OSError as e:
        print(f"[pse] no pude abrir el socket: {e}", file=sys.stderr)
        return

    while not STOP.is_set():
        try:
            pkt = s.recv(512)
        except socket.timeout:
            continue
        except OSError as e:
            print(f"[pse] {e}", file=sys.stderr)
            time.sleep(1)
            continue
        payload = pkt[14:]
        mac = ":".join(f"{b:02x}" for b in pkt[6:12])

        fr = parse_mps_frame(payload)
        if fr is not None:
            # PSE_LIVE se mantiene ADEMAS del registro: /api/pse es la vista
            # historica del unico power switch y hay clientes colgando de ella.
            # Duplicar aqui es barato y evita romperlos.
            with PSE_LOCK:
                PSE_LIVE["ts"] = now()
                PSE_LIVE["mac"] = mac
                PSE_LIVE["puertos"] = fr["puertos"]
                PSE_LIVE["uptime_s"] = fr["uptime_s"]
                PSE_LIVE["seq"] = fr["seq"]
                PSE_LIVE["chip"] = fr.get("chip")
            registrar_switch("mps", mac, fr)
            continue

        fr = parse_mfs_frame(payload)
        if fr is not None:
            registrar_switch("mfs", mac, fr)
            continue
    s.close()


def pse_snapshot() -> dict:
    """Copia de la ultima telemetria, con su antiguedad y la potencia por slot.

    P = Vin x I. La tension sale de `vin_mv`, el Vin que midio la ultima
    clasificacion del LTC4296.

    ⚠️ Ese valor solo se refresca cuando corre una clasificacion, y el MPS
    solo reintenta en los puertos que NO entregan: con los 4 slots entregando
    a la vez, la tension se congela en la ultima medida. Para vatios exactos
    con todo cargado habria que leer Vout por puerto con el GADC.

    ⚠️ Es la tension de ENTRADA, no la de salida. El error es ~0,1 % (a 60 mA
    la resistencia de sensado cae 16 mV sobre 53,8 V), asi que sirve de sobra
    para dimensionar, pero es una estimacion.
    """
    with PSE_LOCK:
        d = dict(PSE_LIVE)
        d["puertos"] = [dict(p) for p in PSE_LIVE["puertos"]]
    d["edad_s"] = (now() - d["ts"]) if d["ts"] else None
    # Sin trama reciente el panel no debe dar por buenos los ultimos mA.
    d["vivo"] = d["edad_s"] is not None and d["edad_s"] <= 10

    # Potencia por slot. SIN DATO NO ES CERO: si falta la corriente o la
    # tension queda None y el panel pinta un guion. Un 0,0 W en un puerto que
    # entrega se leeria como "conectado y sin consumo".
    vin_mv = (d.get("chip") or {}).get("vin_mv")
    total = None
    for p in d["puertos"]:
        ma = p.get("ma")
        if vin_mv is None or ma is None:
            p["w"] = None
        else:
            p["w"] = round(vin_mv * ma / 1e6, 3)
            total = (total or 0.0) + p["w"]
    d["w_total"] = None if total is None else round(total, 3)
    d["w_vin_mv"] = vin_mv          # la tension usada, para poder auditarlo
    return d
# Centinela de "sin referencia" para el nivel (32 bits). La ATT lo manda
# cuando su cuenta arranco sin checkpoint: la calibracion no es aplicable.
ATT_LEVEL_NA = -2147483648


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
    # Sin referencia la CUENTA no significa nada, asi que NINGUNA calibracion
    # es aplicable -- ni la de la ATT ni la de la TPU. Se marca explicitamente
    # en vez de deducirlo de level_mm: en v1 ese campo no existe y vale None
    # sin que eso signifique nada malo.
    ref_ok = True
    if level == ATT_LEVEL_NA:
        level = None
        ref_ok = False
    return {"ver": ver, "tank_id": tank, "seq": seq, "count": count,
            "level_mm": level, "ref_ok": ref_ok, "edges": edges,
            "errors": errors, "uptime": uptime, "temp_c": temp,
            "humi_rh": humi}

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

    def set_cal(self, tank_id: int, scale: float, offset: float,
                unit: str = None):
        """Guarda la recta de calibracion de un tanque.

        Vive en la TPU, no en el sensor: la ATT manda la cuenta cruda y el
        escalado se aplica aqui. Asi, sustituir una placa averiada no
        obliga a recalibrar -- basta con ponerle su tank_id.
        """
        c = self._conn()
        c.execute("INSERT OR IGNORE INTO tanks(tank_id,first_seen,last_seen)"
                  " VALUES(?,?,?)", (tank_id, now(), now()))
        if unit:
            c.execute("UPDATE tanks SET scale=?,offset=?,unit=? WHERE tank_id=?",
                      (scale, offset, unit, tank_id))
        else:
            c.execute("UPDATE tanks SET scale=?,offset=? WHERE tank_id=?",
                      (scale, offset, tank_id))
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
        for tid, name, mac, unit, last_seen, scale, offset in c.execute(
                "SELECT tank_id,name,mac,unit,last_seen,scale,offset FROM tanks"
                " ORDER BY tank_id"):
            seen.add(tid)
            # Periodo OBSERVADO entre las dos ultimas muestras. Es la prueba
            # de que un cambio de periodo llego de verdad al sensor.
            # OJO: samples_raw tiene PK (tank_id, ts) con ts en SEGUNDOS, asi
            # que por debajo de 1 s las tramas se solapan en la misma fila y
            # esto no puede bajar de 1. Para el rango util (1 s - 1 min) vale.
            per = c.execute("SELECT ts FROM samples_raw WHERE tank_id=?"
                            " ORDER BY ts DESC LIMIT 2", (tid,)).fetchall()
            period = (per[0][0] - per[1][0]) if len(per) == 2 else None
            if tid in live:
                r = dict(live[tid])
                r["period_s"] = period
                r["scale"], r["offset"] = scale, offset
                out.append(r)
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
                "period_s": period, "online": False,
                "scale": scale, "offset": offset,
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
            # None = la ATT no tiene referencia: la cuenta no significa nada
            # y aplicarle la recta daria un valor falso pero plausible.
            "value": (count * (c.get("scale") or 1.0) + (c.get("offset") or 0.0)
                      if fr["ref_ok"] else None),
            "ref_ok": fr["ref_ok"],
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
        # Los registros 7/8 salen del mismo att_level_mm(), asi que traen el
        # centinela igual que la trama SPE.
        ref_ok = (level != ATT_LEVEL_NA)
        base = level if cal else count          # calibrado -> nivel; si no, cuenta
        rec = {
            "ts": now(), "tank_id": tank_id, "mac": dev, "seq": 0,
            "count": count, "edges": edges, "errors": errors,
            "uptime_s": uptime_s,
            "value": (base * (c.get("scale") or 1.0) + (c.get("offset") or 0.0)
                      if ref_ok else None),
            "ref_ok": ref_ok,
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
    outs = build_outputs(cfg, live, store, STOP, pse_fn=pse_snapshot,
                         switches_fn=switches_snapshot)

    # Ingesta por dos caminos: SPE (tramas L2) y/o Modbus RTU (RS-485).
    # Con el SPE del ATT roto, el RTU es el que trae los datos.
    if cfg.get("modbus_ingest", {}).get("enabled"):
        threading.Thread(target=ingest_modbus, args=(cfg, store, live, outs),
                         daemon=True, name="rtu").start()
    if cfg["ingest"].get("enabled", True):
        threading.Thread(target=ingest, args=(cfg, store, live, outs),
                         daemon=True, name="spe").start()
        # Hilo aparte para la telemetria del MPS: si falla, la ingesta de
        # tanques -- que es la critica -- no se entera.
        threading.Thread(target=ingest_pse, args=(cfg,),
                         daemon=True, name="pse").start()

    while not STOP.is_set():
        time.sleep(0.5)
    print("[gw] fin")


if __name__ == "__main__":
    main()
