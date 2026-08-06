#!/usr/bin/env python3
"""Configuracion remota de los sensores ATT por Modbus TCP.

Permite cambiar el tank_id y el periodo de muestreo de un sensor desde la UI,
sin destapar el Varec ni recompilar firmware. Es la contraparte de los holding
registers que implementa la ATT:

    HR 0  flags        HR 3  paridad RTU
    HR 1  unit_id      HR 4  periodo del push (decenas de ms)
    HR 2  baud/100     HR 5  tank_id
    HR 9  comando: 0xA5 = guardar en EEPROM, 0x5A = valores de fabrica

Los cambios se aplican al reiniciar el sensor (comportamiento habitual en
equipo industrial), salvo el tank_id, que la ATT usa en la siguiente trama.

Se habla Modbus con sockets crudos en vez de arrastrar pymodbus: solo hacen
falta FC 03/06 contra un puerto conocido.
"""
import glob
import os
import re
import socket
import struct

MB_PORT = 502
HR_FLAGS = 0
HR_UNIT_ID = 1
HR_PUSH_MS10 = 4
HR_TANK_ID = 5
HR_CMD = 9
CMD_SAVE = 0xA5
CMD_FACTORY = 0x5A


# ---------------------------------------------------------------- MAC -> IP
def mac_to_ip(mac: str) -> str:
    """La pasarela conoce la MAC de cada ATT (viene en la trama), pero para
    hablarle Modbus hace falta su IP. Se busca en las concesiones DHCP que
    reparte la propia CM5, y si no, en la tabla ARP."""
    mac = mac.lower()

    for f in glob.glob("/var/lib/NetworkManager/dnsmasq-*.leases"):
        try:
            with open(f) as fh:
                for line in fh:
                    p = line.split()
                    if len(p) >= 3 and p[1].lower() == mac:
                        return p[2]
        except OSError:
            continue

    try:
        with open("/proc/net/arp") as fh:
            next(fh)
            for line in fh:
                p = line.split()
                if len(p) >= 4 and p[3].lower() == mac:
                    return p[0]
    except OSError:
        pass
    return ""


# ---------------------------------------------------------------- Modbus
class Sesion:
    """UNA sola conexion TCP para varias transacciones seguidas.

    ⚠️ ABRIR UNA CONEXION POR TRANSACCION AGOTA LOS CONTEXTOS TCP DE LA ATT.
    Ya paso dos veces en este proyecto: tras unas pocas operaciones seguidas el
    sensor deja de aceptar conexiones y todo da tiempo de espera agotado --
    parece que el sensor se ha caido, y lo que se ha caido es su tabla de
    contextos. Se recupera solo cuando caducan, pero mientras tanto no hay
    forma de hablar con el.

    Cualquier operacion de varios pasos (leer-modificar-escribir, o escribir y
    guardar en EEPROM) DEBE ir por una sola sesion.

    ⚠️ Ademas se correlaciona por transaction id: con una conexion persistente,
    reintentar sin comprobar el tid desincroniza el flujo y se acaba leyendo la
    respuesta de la peticion anterior como si fuera la actual.
    """

    def __init__(self, ip: str, unit: int = 1, timeout: float = 4.0):
        self.unit = unit
        self.tid = 0
        self.s = socket.create_connection((ip, MB_PORT), timeout=timeout)

    def _leer(self, n: int) -> bytes:
        """Lee EXACTAMENTE n bytes.

        ⚠️ recv(n) puede devolver MENOS: TCP es un flujo de bytes, no de
        mensajes, y no garantiza que una respuesta llegue en un solo segmento.
        Dar por hecho lo contrario produce un fallo intermitente y
        desconcertante -- "respuesta incompleta" sobre un sensor que esta
        contestando perfectamente.
        """
        buf = b""
        while len(buf) < n:
            trozo = self.s.recv(n - len(buf))
            if not trozo:
                raise IOError("conexion cerrada por el sensor")
            buf += trozo
        return buf

    def txn(self, pdu: bytes) -> bytes:
        self.tid = (self.tid % 0xFFFF) + 1
        req = struct.pack(">HHHB", self.tid, 0, len(pdu) + 1, self.unit) + pdu
        self.s.sendall(req)
        h = self._leer(7)
        tid, _, ln, _ = struct.unpack(">HHHB", h)
        body = self._leer(ln - 1)
        if tid != self.tid:
            raise IOError(f"respuesta descolocada (tid {tid} != {self.tid})")
        if body and (body[0] & 0x80):
            raise IOError(f"excepcion Modbus {body[1] if len(body) > 1 else '?'}")
        return body

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _txn(ip: str, unit: int, pdu: bytes, timeout=4.0) -> bytes:
    """Una transaccion suelta, con su propia conexion.

    ⚠️ Para operaciones de VARIOS pasos NO usar esto en bucle: una conexion por
    transaccion agota los contextos TCP de la ATT y la deja fuera de servicio
    hasta que alguien va a reiniciarla. Usar Sesion.
    """
    with Sesion(ip, unit, timeout) as ses:
        return ses.txn(pdu)


def read_cfg(ip: str, unit: int = 1) -> dict:
    """Lee los holding registers de configuracion del sensor."""
    body = _txn(ip, unit, struct.pack(">BHH", 3, 0, 6))
    if len(body) < 2 + 12:
        raise IOError("respuesta incompleta")
    r = struct.unpack(">6H", body[2:14])
    return {"flags": r[0], "unit_id": r[1], "baud": r[2] * 100,
            "parity": r[3], "push_ms": r[4] * 10, "tank_id": r[5]}


def write_reg(ip: str, addr: int, val: int, unit: int = 1):
    _txn(ip, unit, struct.pack(">BHH", 6, addr, val))


def set_tank_id(ip: str, tank_id: int, unit: int = 1, save=True):
    """Asigna la identidad del tanque y la persiste en la EEPROM."""
    if not (0 <= tank_id <= 65535):
        raise ValueError("tank_id fuera de rango")
    write_reg(ip, HR_TANK_ID, tank_id, unit)
    if save:
        # Sin esto el cambio vive solo en RAM y se pierde al reiniciar.
        write_reg(ip, HR_CMD, CMD_SAVE, unit)


# Banderas de HR 0. Espejo de CFG_F_* en att/src/main.c.
F_PUSH     = 0x01   # push L2 por SPE
F_RTU      = 0x02   # puerto RS-485 (Modbus RTU)
F_NO_K1    = 0x04   # no energizar el rele K1
F_ENC_LEDS = 0x08   # LEDs de canal del encoder (diagnostico)
F_BAT      = 0x10   # suspender transmisiones con la placa en bateria
#
# ⚠️ F_BAT NO SE ACTIVA A CIEGAS. PB11 no significa lo mismo en todas las
# placas: medido, tank1 da 0 con alimentacion externa y tank21 da 1 en la
# MISMA situacion. En una placa invertida esta bandera la deja MUDA nada mas
# arrancar, e indistinguible de un sensor averiado.
#
# ANTES DE ACTIVARLA, comprobar en ESA placa que con alimentacion externa el
# arbol reporta pwr.gpio_listo = true y pwr.pb11 = false (trama v4).


def set_flag(ip: str, mask: int, on: bool, unit: int = 1, save=True):
    """Cambia UNA bandera de HR 0 sin tocar las demas.

    ⚠️ Se LEE antes de escribir. HR 0 es un registro con varias banderas: mandar
    solo la que se quiere cambiar apagaria las otras -- incluida F_NO_K1, que en
    una placa con SJ1 puenteado dejaria el rele energizandose y el sensor sin
    enlace de red.

    ⚠️ El RS-485 y los LEDs surten efecto AL INSTANTE en el firmware; `save`
    solo decide si ademas sobrevive a un reinicio. Para el modo diagnostico de
    los LEDs interesa NO guardar: se enciende para mirar y se olvida apagarlo.
    """
    with Sesion(ip, unit) as ses:
        body = ses.txn(struct.pack(">BHH", 3, HR_FLAGS, 1))
        if len(body) < 4:
            raise IOError("respuesta incompleta al leer HR 0")
        cur = struct.unpack(">H", body[2:4])[0]
        new = (cur | mask) if on else (cur & ~mask)
        if new == cur:
            return cur
        ses.txn(struct.pack(">BHH", 6, HR_FLAGS, new))
        if save:
            ses.txn(struct.pack(">BHH", 6, HR_CMD, CMD_SAVE))
    return new


def set_push_ms(ip: str, ms: int, unit: int = 1, save=True):
    """Periodo de muestreo. Un Varec mide nivel de liquido: se mueve en
    minutos, no en milisegundos. 30 s es un punto sensato."""
    v = ms // 10
    if not (5 <= v <= 6000):            # 50 ms .. 60 s
        raise ValueError("periodo fuera de rango (50ms-60s)")
    write_reg(ip, HR_PUSH_MS10, v, unit)
    if save:
        write_reg(ip, HR_CMD, CMD_SAVE, unit)
