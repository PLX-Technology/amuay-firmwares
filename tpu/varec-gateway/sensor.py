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
def _txn(ip: str, unit: int, pdu: bytes, timeout=4.0) -> bytes:
    s = socket.create_connection((ip, MB_PORT), timeout=timeout)
    try:
        req = struct.pack(">HHHB", 1, 0, len(pdu) + 1, unit) + pdu
        s.sendall(req)
        h = s.recv(7)
        if len(h) < 7:
            raise IOError("respuesta corta")
        _, _, ln, _ = struct.unpack(">HHHB", h)
        body = s.recv(ln - 1)
        if body and (body[0] & 0x80):
            raise IOError(f"excepcion Modbus {body[1] if len(body) > 1 else '?'}")
        return body
    finally:
        s.close()


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


def set_push_ms(ip: str, ms: int, unit: int = 1, save=True):
    """Periodo de muestreo. Un Varec mide nivel de liquido: se mueve en
    minutos, no en milisegundos. 30 s es un punto sensato."""
    v = ms // 10
    if not (5 <= v <= 6000):            # 50 ms .. 60 s
        raise ValueError("periodo fuera de rango (50ms-60s)")
    write_reg(ip, HR_PUSH_MS10, v, unit)
    if save:
        write_reg(ip, HR_CMD, CMD_SAVE, unit)
