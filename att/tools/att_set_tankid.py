#!/usr/bin/env python3
"""Asigna el tank_id a una ATT por Modbus TCP.

MAPA (verificado en att/src/main.c:435-436)
  HR 5 : tank_id
  HR 9 : comando -> 0xA5 = guardar en EEPROM

⚠️ La escritura del HR 5 NO persiste sola. El propio firmware lo avisa:
"No guarda solo: revisar con FC03 y luego 0xA5 para persistir".

⚠️ UNA CONEXION POR TRANSACCION, a proposito.
Esta ATT pierde peticiones sueltas: se queda sin buffers de red por el trafico
de difusion de la oficina que le llega por el segmento SPE. Hay que reintentar.
Pero reintentar sobre la MISMA conexion desincroniza el flujo: la respuesta
tardia de la peticion perdida llega despues y todas las lecturas siguientes
salen corridas. Paso por eso: una relectura devolvio 1280 (0x0500), que eran
bytes de otra respuesta, no el registro.
Reconectar por transaccion, y ademas comprobar el identificador de
transaccion, elimina la ambiguedad. Es mas lento y da igual: son 4 peticiones.
"""
import socket, struct, sys

HOST = sys.argv[1] if len(sys.argv) > 1 else '172.16.10.105'
NUEVO = int(sys.argv[2]) if len(sys.argv) > 2 else 1
UNIT = int(sys.argv[3]) if len(sys.argv) > 3 else 1

HR_TANK_ID = 5
HR_CMD = 9
CMD_GUARDAR = 0xA5

_tid = 0


def _exacto(s, n):
    """TCP es un flujo: la respuesta llega troceada. Insistir hasta completar."""
    buf = b''
    while len(buf) < n:
        t = s.recv(n - len(buf))
        if not t:
            raise IOError('conexion cerrada a media respuesta')
        buf += t
    return buf


def pedir(pdu, reintentos=5):
    global _tid
    ultimo = None
    for intento in range(reintentos):
        _tid = (_tid + 1) & 0xFFFF
        try:
            s = socket.create_connection((HOST, 502), timeout=6)
            s.settimeout(6)
            try:
                s.sendall(struct.pack('!HHHB', _tid, 0, len(pdu) + 1, UNIT) + pdu)
                cab = _exacto(s, 7)
                tid, proto, ln, unit = struct.unpack('!HHHB', cab)
                cuerpo = _exacto(s, ln - 1)
            finally:
                s.close()
            if tid != _tid:
                raise IOError('respuesta descolocada: tid %d, esperaba %d' % (tid, _tid))
            if cuerpo[0] & 0x80:
                raise IOError('excepcion Modbus 0x%02x en funcion 0x%02x'
                              % (cuerpo[1], cuerpo[0] & 0x7F))
            return cuerpo
        except (socket.timeout, IOError, OSError) as e:
            ultimo = e
            if intento < reintentos - 1:
                print('  (%s; reintento %d)' % (type(e).__name__, intento + 2))
    raise ultimo


def leer(addr):
    r = pedir(struct.pack('!BHH', 3, addr, 1))
    return struct.unpack('!H', r[2:4])[0]


def escribir(addr, val):
    pedir(struct.pack('!BHH', 6, addr, val))


print('ATT %s  unit_id %d' % (HOST, UNIT))
antes = leer(HR_TANK_ID)
print('tank_id actual  : %d' % antes)

if antes != NUEVO:
    print('escribiendo HR %d = %d ...' % (HR_TANK_ID, NUEVO))
    escribir(HR_TANK_ID, NUEVO)
    ahora = leer(HR_TANK_ID)
    print('releido en RAM  : %d  %s' % (ahora, 'OK' if ahora == NUEVO else '*** NO COINCIDE'))
    if ahora != NUEVO:
        print('>>> el registro no acepto el valor; NO se guarda en EEPROM')
        sys.exit(1)
else:
    print('  (ya estaba en %d; se continua para asegurar el guardado)' % NUEVO)

print('guardando en EEPROM (HR %d = 0x%02X) ...' % (HR_CMD, CMD_GUARDAR))
escribir(HR_CMD, CMD_GUARDAR)

final = leer(HR_TANK_ID)
print('tras guardar    : %d  %s' % (final, 'OK' if final == NUEVO else '*** NO COINCIDE'))
print()
print('La persistencia REAL solo la prueba un POR: si tras reiniciar la consola')
print('dice tank_id=%d, quedo en la EEPROM.' % NUEVO)
