#!/usr/bin/env python3
"""Persiste la configuracion de la ATT en su EEPROM (HR 9 = 0xA5).

⚠️ UNA SOLA CONEXION, y reintentos SOBRE ELLA.
Este dispositivo obliga a un equilibrio incomodo y las dos salidas faciles
fallan:

  - Reintentar sobre la misma conexion SIN correlacionar -> la respuesta
    tardia de la peticion perdida llega despues y desincroniza el flujo.
    Paso: una relectura devolvio 1280 (0x0500), bytes de otra respuesta.

  - Reconectar en cada reintento -> se agotan los contextos TCP de una pila
    de Zephyr pequeña (y esta ATT ademas malgasta buffers con la difusion de
    la red de oficina). Paso: dejo de aceptar conexiones del todo.

La salida buena es una conexion persistente CON identificador de transaccion:
si llega una respuesta vieja se descarta y se sigue leyendo, que es
exactamente para lo que existe ese campo del protocolo.
"""
import socket, struct, sys, time

HOST = sys.argv[1] if len(sys.argv) > 1 else '172.16.10.105'
UNIT = int(sys.argv[2]) if len(sys.argv) > 2 else 1

HR_TANK_ID = 5
HR_CMD = 9
CMD_GUARDAR = 0xA5

_tid = 0
_s = None


def conectar(intentos=6):
    global _s
    for i in range(intentos):
        try:
            _s = socket.create_connection((HOST, 502), timeout=8)
            _s.settimeout(8)
            print('conectado a %s:502' % HOST)
            return
        except OSError as e:
            print('  no acepta conexion (%s); esperando a que se recupere...'
                  % type(e).__name__)
            time.sleep(5)
    raise SystemExit('>>> la ATT no acepta conexiones. Dale un POR y reintenta.')


def _exacto(n, hasta):
    buf = b''
    while len(buf) < n:
        _s.settimeout(max(0.1, hasta - time.time()))
        t = _s.recv(n - len(buf))
        if not t:
            raise IOError('conexion cerrada')
        buf += t
    return buf


def pedir(pdu, reintentos=5):
    global _tid
    for intento in range(reintentos):
        _tid = (_tid + 1) & 0xFFFF
        mio = _tid
        _s.sendall(struct.pack('!HHHB', mio, 0, len(pdu) + 1, UNIT) + pdu)
        hasta = time.time() + 8
        try:
            while time.time() < hasta:
                cab = _exacto(7, hasta)
                tid, _, ln, _u = struct.unpack('!HHHB', cab)
                cuerpo = _exacto(ln - 1, hasta)
                if tid != mio:
                    # Respuesta atrasada de un intento anterior: se tira y se
                    # sigue leyendo. Esto es lo que mantiene el flujo alineado.
                    print('  (descartada respuesta atrasada tid=%d)' % tid)
                    continue
                if cuerpo[0] & 0x80:
                    raise IOError('excepcion Modbus 0x%02x' % cuerpo[1])
                return cuerpo
        except socket.timeout:
            pass
        if intento < reintentos - 1:
            print('  (sin respuesta; reintento %d)' % (intento + 2))
    raise IOError('sin respuesta tras %d intentos' % reintentos)


def leer(addr):
    return struct.unpack('!H', pedir(struct.pack('!BHH', 3, addr, 1))[2:4])[0]


def escribir(addr, val):
    pedir(struct.pack('!BHH', 6, addr, val))


conectar()
tid_actual = leer(HR_TANK_ID)
print('tank_id en RAM  : %d' % tid_actual)
if tid_actual == 0:
    raise SystemExit('>>> tank_id es 0: escribelo antes de guardar.')

print('guardando en EEPROM (HR %d = 0x%02X) ...' % (HR_CMD, CMD_GUARDAR))
escribir(HR_CMD, CMD_GUARDAR)
print('tras guardar    : %d' % leer(HR_TANK_ID))
_s.close()
print()
print('La persistencia REAL solo la prueba un POR: si tras reiniciar la consola')
print('dice tank_id=%d, quedo en la EEPROM.' % tid_actual)
