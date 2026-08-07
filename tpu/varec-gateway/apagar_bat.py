#!/usr/bin/env python3
"""Apaga CFG_F_BAT (suspension por bateria) en una ATT. Espera a que responda.

    python3 apagar_bat.py 192.168.50.7

POR QUE HACE FALTA ESPERAR: si la placa ya se suspendio, esta MUDA -- sin SPE,
sin RS-485 y sin Modbus -- asi que no hay forma de apagarle la bandera. La
unica via es la ventana en la que esta despierta:

  - ponle la bateria (con bateria PB11 baja y la placa despierta), o
  - reinicia su alimentacion SPE y pilla el arranque.

Esto se queda llamando cada 200 ms y actua en cuanto contesta.

CUANDO USARLO: en toda placa cuya PB11 no este verificada EN LOS DOS ESTADOS
(con potencia SPE y sin ella). Un pin que solo delata la presencia de la
bateria da 0 con alimentacion externa -- parece correcto -- pero se va a ALTO
al quitar la bateria, y entonces la placa se calla con el SPE energizado.
Medido en tank7 el 2026-08-07.
"""
import socket, struct, sys, time

IP = sys.argv[1]
F_BAT = 0x10


def txn(s, pdu):
    s.sendall(struct.pack(">HHHB", 1, 0, len(pdu) + 1, 1) + pdu)
    h = s.recv(7)
    if len(h) < 7:
        raise IOError("respuesta corta")
    ln = struct.unpack(">H", h[4:6])[0]
    b = b""
    while len(b) < ln - 1:
        t = s.recv(ln - 1 - len(b))
        if not t:
            raise IOError("cerrada")
        b += t
    if b[0] & 0x80:
        raise IOError("excepcion Modbus 0x%02x" % b[1])
    return b


def leer(s, f, a, n):
    b = txn(s, struct.pack(">BHH", f, a, n))
    return struct.unpack(">%dH" % (b[1] // 2), b[2:2 + b[1]])


print("esperando a que %s responda..." % IP)
print("  ponle la bateria, o reinicia su alimentacion SPE. Ctrl-C para dejarlo.")
t0 = time.time()
s = None
while s is None:
    try:
        s = socket.create_connection((IP, 502), timeout=0.4)
    except OSError:
        time.sleep(0.2)

s.settimeout(4)
print("responde tras %.1f s" % (time.time() - t0))
try:
    hr0 = leer(s, 3, 0, 1)[0]
    try:
        pwr = leer(s, 4, 12, 1)[0]
        print("  IR 12 = 0x%04X -> pb11=%d en_bateria=%d bat_activa=%d gpio_listo=%d"
              % (pwr, pwr & 1, (pwr >> 1) & 1, (pwr >> 2) & 1, (pwr >> 3) & 1))
    except Exception as e:
        print("  IR 12 no disponible (%s)" % e)
    if not (hr0 & F_BAT):
        print("  HR 0 = 0x%04X: CFG_F_BAT ya estaba apagada, no se toca" % hr0)
    else:
        txn(s, struct.pack(">BHH", 6, 0, hr0 & ~F_BAT))
        txn(s, struct.pack(">BHH", 6, 9, 0xA5))     # persistir en EEPROM
        time.sleep(0.4)
        print("  HR 0: 0x%04X -> 0x%04X   CFG_F_BAT APAGADA y guardada"
              % (hr0, leer(s, 3, 0, 1)[0]))
        print("  -> esta placa ya reporta lleve bateria o no")
finally:
    s.close()
