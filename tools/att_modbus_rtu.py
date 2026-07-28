#!/usr/bin/env python3
"""Maestro Modbus RTU minimo para probar el RS-485 de la ATT.

La ATT es esclavo (unit id 1) a 19200 8E1. Su RE/DE van atados, asi que
NO hay eco local: hace falta este segundo nodo para que la prueba valga.
"""
import serial, struct, sys, time

PORT = sys.argv[1] if len(sys.argv) > 1 else '/dev/ttyAMA2'
UNIT = int(sys.argv[2]) if len(sys.argv) > 2 else 1


def crc16(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def ask(ser, func, addr, qty):
    req = struct.pack('>BBHH', UNIT, func, addr, qty)
    req += struct.pack('<H', crc16(req))
    ser.reset_input_buffer()
    ser.write(req)
    ser.flush()
    time.sleep(0.15)
    resp = ser.read(256)
    if not resp:
        return None, 'sin respuesta (timeout)'
    if len(resp) < 5:
        return None, 'respuesta corta: ' + resp.hex(' ')
    if crc16(resp[:-2]) != struct.unpack('<H', resp[-2:])[0]:
        return None, 'CRC malo: ' + resp.hex(' ')
    if resp[1] & 0x80:
        return None, 'excepcion Modbus 0x%02x' % resp[2]
    n = resp[2]
    return struct.unpack('>%dH' % (n // 2), resp[3:3 + n]), None


ser = serial.Serial(PORT, 19200, parity=serial.PARITY_EVEN,
                    stopbits=1, bytesize=8, timeout=1.0)
print('puerto %s a 19200 8E1, esclavo unit id %d' % (PORT, UNIT))

for func, addr, qty, label in ((3, 0, 6, 'holding (config)'),
                               (4, 0, 7, 'input (medida)')):
    vals, err = ask(ser, func, addr, qty)
    if err:
        print('  FC%02d %-18s -> %s' % (func, label, err))
    else:
        print('  FC%02d %-18s -> %s' % (func, label,
              ' '.join('%d' % v for v in vals)))
ser.close()
