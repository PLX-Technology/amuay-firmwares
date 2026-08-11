#!/usr/bin/env python3
"""Actualizacion de firmware de las ATT por SPE, desde la pasarela.

Envuelve `smpmgr` (mcumgr/SMP sobre UDP) en tres pasos y una verificacion:

    subir a slot1  ->  marcar pendiente  ->  reiniciar  ->  comprobar slot0

⚠️ `smpmgr upgrade` NO marca la imagen: sube y reinicia, y la placa arranca la
de siempre. Por eso van los tres pasos por separado.

⚠️ SE VERIFICA LEYENDO LA PLACA, no dando por bueno que el comando no fallara.
Al terminar se relee slot0 y se compara su hash con el del fichero. Un OTA que
dice "actualizado" sin comprobarlo es peor que no tenerlo: en un tanque nadie
va a ir a mirar.

La red de seguridad vive en el firmware de la ATT y no hace falta tocarla
desde aqui: MCUboot arranca la imagen nueva A PRUEBA, y la ATT solo se
autoconfirma tras 4 envios SPE correctos seguidos. Una imagen que arranque
pero no transmita se revierte sola en el siguiente reinicio.
"""
import hashlib
import os
import re
import struct
import subprocess
import threading
import time

IMG_MAGIC = 0x96F3B83D
TLV_INFO_MAGIC = (0x6907, 0x6908)
TLV_SHA256 = 0x10

# ⚠️ SIEMPRE 512, aunque el firmware nuevo aguante mas.
#
# El MTU por defecto manda ~1448 B de datos por paquete, y una placa con el
# buffer viejo (CONFIG_MCUMGR_TRANSPORT_NETBUF_SIZE=1024) corrompe UN byte por
# paquete -- escribe encima 0xBF, el marcador CBOR de inicio de su respuesta.
# La subida llega al 100 %, el hash que reporta la placa es correcto, y solo
# falla al final, cuando MCUboot rehace el calculo sobre la flash. Costo una
# jornada localizarlo (ver att/OTA-Y-TRAMA-V3.md §5).
#
# A 512 funciona con el firmware viejo Y con el nuevo. La diferencia es 5 kB/s
# frente a 7,4: unos diez segundos mas en una imagen de 150 KB. No merece la
# pena arriesgar un sensor de campo por eso.
MTU = 512

SMPMGR_DEF = "/home/tpu01/smpvenv/bin/smpmgr"
DIR_IMG_DEF = "/var/lib/varec-gateway/ota"


# ------------------------------------------------------------ la imagen
def analizar(data: bytes) -> dict:
    """Comprueba que el fichero es una imagen firmada de MCUboot y coherente.

    Se hace ANTES de ofrecer nada: mandar un fichero equivocado a un sensor
    dentro de un tanque es justo el error que no se puede permitir. Un .bin
    truncado, el binario sin firmar, o la imagen combinada con MCUboot delante
    (la del grabado por cable) se cazan aqui y no llegan a la placa.

    El hash de MCUboot NO es el del fichero entero: es sobre cabecera +
    payload + TLV protegidos, y se compara con el que la propia imagen lleva
    guardado en su TLV 0x10.
    """
    r = {"ok": False, "motivo": "", "sha": None, "tam": len(data), "version": None}
    if len(data) < 32:
        r["motivo"] = "fichero demasiado corto para ser una imagen"
        return r
    magic, _load, hdr, ptlv, imgsz, _flags = struct.unpack("<IIHHII", data[:20])
    vmaj, vmin, vrev, vbld = struct.unpack("<BBHI", data[20:28])
    if magic != IMG_MAGIC:
        r["motivo"] = ("no lleva la cabecera de MCUboot (magic %08x). ¿Es el "
                       "zephyr.bin sin firmar, o la imagen combinada del "
                       "grabado por cable?" % magic)
        return r
    fin = hdr + imgsz
    if fin + 4 > len(data):
        r["motivo"] = "fichero truncado: la cabecera anuncia mas de lo que hay"
        return r
    tm, tt = struct.unpack("<HH", data[fin:fin + 4])
    if tm not in TLV_INFO_MAGIC:
        r["motivo"] = "no se encuentra la zona de TLV donde deberia"
        return r
    if fin + tt > len(data):
        r["motivo"] = "fichero truncado: la zona de TLV no cabe entera"
        return r

    calc = hashlib.sha256(data[:hdr + imgsz + ptlv]).hexdigest().upper()
    guardado, p = None, fin + 4
    while p + 4 <= fin + tt:
        ty, ln = struct.unpack("<HH", data[p:p + 4])
        if ty == TLV_SHA256:
            guardado = data[p + 4:p + 4 + ln].hex().upper()
            break
        p += 4 + ln
    if guardado is None:
        r["motivo"] = "la imagen no lleva TLV de hash"
        return r
    if guardado != calc:
        r["motivo"] = ("el hash no cuadra con el contenido: el fichero esta "
                       "corrupto o incompleto")
        return r

    r.update(ok=True, sha=calc, version="%d.%d.%d+%d" % (vmaj, vmin, vrev, vbld),
             img_size=imgsz)
    return r


# ------------------------------------------------------------ smpmgr
def _base(cfg, ip):
    exe = ((cfg or {}).get("ota") or {}).get("smpmgr") or SMPMGR_DEF
    return [exe, "--ip", ip, "--mtu", str(MTU), "--timeout", "10"]


def _corre(cmd, tmo):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=tmo)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, "tiempo de espera agotado"
    except FileNotFoundError:
        return -1, "no se encuentra smpmgr (revisa ota.smpmgr en la config)"
    except Exception as e:                                  # noqa: BLE001
        return -1, str(e)


_RE_SLOT = re.compile(
    r"slot=(\d).*?hash=HashBytes\(\s*.([0-9A-Fa-f]+).*?"
    r"confirmed=(\w+),\s*active=(\w+)", re.S)


def ranuras(ip, cfg) -> list:
    """Lee el estado de las ranuras. Devuelve [] si la placa no contesta."""
    rc, out = _corre(_base(cfg, ip) + ["image", "state-read"], 40)
    if rc != 0 and "slot=" not in out:
        return []
    return [{"slot": int(m.group(1)), "sha": m.group(2).upper(),
             "confirmada": m.group(3) == "True", "activa": m.group(4) == "True"}
            for m in _RE_SLOT.finditer(out)]


def activa(ip, cfg):
    """Hash de la imagen que la placa esta ejecutando, o None."""
    for r in ranuras(ip, cfg):
        if r["activa"]:
            return r["sha"]
    return None


# ------------------------------------------------------------ el trabajo
class Trabajo:
    """Una actualizacion en marcha. La UI la sigue por `snapshot()`.

    Va en un hilo porque la secuencia tarda cerca de un minuto y medio, y una
    peticion HTTP abierta todo ese rato es una invitacion a que el navegador
    se rinda a mitad y nadie sepa como quedo la placa.
    """

    def __init__(self, tank_id, ip, ruta, sha, cfg):
        self.tank_id, self.ip, self.ruta, self.sha, self.cfg = tank_id, ip, ruta, sha, cfg
        self.fase = "esperando"
        self.estado = "en_curso"
        self.motivo = ""
        self.ts = time.time()
        self._lock = threading.Lock()

    def _fase(self, f):
        with self._lock:
            self.fase = f

    def snapshot(self):
        with self._lock:
            return {"tank_id": self.tank_id, "ip": self.ip, "fase": self.fase,
                    "estado": self.estado, "motivo": self.motivo,
                    "sha": self.sha, "ts": self.ts}

    def correr(self):
        base = _base(self.cfg, self.ip)
        try:
            self._fase("comprobando la placa")
            if not ranuras(self.ip, self.cfg):
                return self._falla("la placa no responde por SPE")

            self._fase("subiendo la imagen")
            rc, out = _corre(base + ["image", "upload", self.ruta], 900)
            if rc != 0:
                return self._falla("fallo la subida: %s" % out.strip()[-200:])

            self._fase("marcando la imagen")
            rc, out = _corre(base + ["image", "state-write", self.sha], 60)
            if rc != 0:
                return self._falla("no se pudo marcar: %s" % out.strip()[-200:])
            if not any(r["sha"] == self.sha for r in ranuras(self.ip, self.cfg)):
                return self._falla("la imagen no aparece en la placa tras subirla")

            self._fase("reiniciando")
            _corre(base + ["os", "reset"], 30)

            # El intercambio tarda unos segundos y luego arranca Zephyr y sube
            # el enlace SPE. Se sondea en vez de dormir a ciegas.
            self._fase("esperando a que vuelva")
            act = None
            for _ in range(24):
                time.sleep(5)
                act = activa(self.ip, self.cfg)
                if act:
                    break
            if not act:
                return self._falla("no volvio a responder tras el reinicio")

            # ★ LA COMPROBACION QUE VALE: que la placa este CORRIENDO la nueva.
            if act != self.sha:
                return self._falla(
                    "volvio con la imagen anterior (%s...): MCUboot rechazo la "
                    "nueva o la revirtio. El sensor sigue funcionando."
                    % act[:16])
            with self._lock:
                self.fase, self.estado = "actualizada y verificada", "ok"
        except Exception as e:                              # noqa: BLE001
            self._falla("error inesperado: %s" % e)

    def _falla(self, motivo):
        with self._lock:
            self.estado, self.motivo, self.fase = "error", motivo, "terminado"


class Gestor:
    """Guarda la imagen subida y deja correr UN trabajo a la vez.

    De uno en uno a proposito. Con 50 tanques la tentacion de "actualizar
    todos" es fuerte, y tambien lo es la posibilidad de dejar 50 sensores raros
    a la vez. Que el operador vea uno terminar antes de lanzar el siguiente.
    """

    def __init__(self, cfg):
        self.cfg = cfg or {}
        d = ((cfg or {}).get("ota") or {}).get("dir") or DIR_IMG_DEF
        self.dir = d
        self.ruta = os.path.join(d, "att.signed.bin")
        self.meta_path = os.path.join(d, "att.meta")
        self.trabajo = None
        self._lock = threading.Lock()

    # -- imagen guardada
    def guardar(self, data: bytes) -> dict:
        info = analizar(data)
        if not info["ok"]:
            return info
        os.makedirs(self.dir, exist_ok=True)
        tmp = self.ruta + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, self.ruta)      # atomico: nunca una imagen a medias
        with open(self.meta_path, "w") as f:
            f.write("%s %d %s %d\n" % (info["sha"], info["tam"],
                                       info["version"], int(time.time())))
        return info

    def imagen(self):
        try:
            sha, tam, ver, ts = open(self.meta_path).read().split()
            if not os.path.exists(self.ruta):
                return None
            return {"sha": sha, "tam": int(tam), "version": ver, "ts": int(ts)}
        except Exception:                                   # noqa: BLE001
            return None

    # -- trabajos
    def lanzar(self, tank_id, ip):
        img = self.imagen()
        if not img:
            return {"error": "no hay imagen cargada"}
        with self._lock:
            if self.trabajo and self.trabajo.snapshot()["estado"] == "en_curso":
                return {"error": "ya hay una actualizacion en curso (tanque %s)"
                                 % self.trabajo.tank_id}
            t = Trabajo(tank_id, ip, self.ruta, img["sha"], self.cfg)
            self.trabajo = t
        threading.Thread(target=t.correr, daemon=True).start()
        return {"ok": True, "trabajo": t.snapshot()}

    def estado(self):
        t = self.trabajo
        return {"imagen": self.imagen(),
                "trabajo": t.snapshot() if t else None}
