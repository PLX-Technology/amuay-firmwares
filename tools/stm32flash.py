"""Flasher del STM32 por el bootloader UART (AN3155), para la tarjeta ATT.

El bootloader ROM de esta placa es INTERMITENTE: falla ~1 de cada 2 con un
error de escritura. Por eso:
  - cada bloque se reintenta,
  - y al final se RELEE el flash para comparar (Read Memory, 0x11).

La verificacion no es opcional: sin ella, un bloque mal escrito que igual
devuelve ACK pasa desapercibido y deja la placa sin arrancar, pareciendo
un bug de firmware.
"""
import serial, sys, time

BINF = sys.argv[1] if len(sys.argv) > 1 else 'att_blinky.bin'
PORT = sys.argv[2] if len(sys.argv) > 2 else 'COM6'   # el puerto cambia al reenchufar
ADDR = 0x08000000


def ack(ser, tmo=2):
    ser.timeout = tmo
    return ser.read(1) == b'\x79'


def cmd(ser, c):
    ser.reset_input_buffer()
    ser.write(bytes([c, c ^ 0xFF]))
    return ack(ser)


def connect(ser):
    for _ in range(25):
        ser.reset_input_buffer()
        ser.write(b'\x7f')
        r = ser.read(1)
        if r in (b'\x79', b'\x1f'):
            return True
        time.sleep(0.3)
    return False


def erase_all(ser):
    if not cmd(ser, 0x44):
        return False
    ser.write(bytes([0xFF, 0xFF, 0x00]))   # 0xFFFF = mass erase, checksum 0x00
    return ack(ser, tmo=40)


def send_addr(ser, addr):
    a = addr.to_bytes(4, 'big')
    ser.write(a + bytes([a[0] ^ a[1] ^ a[2] ^ a[3]]))
    return ack(ser)


def write_mem(ser, addr, data):
    """Escribe un bloque. Insiste, y RESINCRONIZA antes de rendirse.

    El fallo tipico no es del flash sino de la LINEA SERIE: los picos de
    corriente de la escritura hunden el rail del que cuelga el VCCIO del
    FT230X (via R84) y se pierde un byte. Tras eso el bootloader sigue vivo,
    solo hace falta volver a sincronizar. Rendirse al 4o intento tiraba a la
    basura los 209 KB enteros por culpa de un solo bloque.
    """
    for intento in range(8):
        if cmd(ser, 0x31) and send_addr(ser, addr):
            n = len(data)
            ck = (n - 1)
            for b in data:
                ck ^= b
            ser.write(bytes([n - 1]) + data + bytes([ck & 0xFF]))
            if ack(ser, tmo=3):
                return True
        ser.reset_input_buffer()
        time.sleep(0.05 * (intento + 1))     # espera creciente
        if intento >= 2:                     # a partir del 3o, resincronizar
            connect(ser)
    return False


def read_mem(ser, addr, n):
    """Read Memory (0x11). Devuelve n bytes o None."""
    for _ in range(4):
        if cmd(ser, 0x11) and send_addr(ser, addr):
            ser.write(bytes([n - 1, (n - 1) ^ 0xFF]))
            if ack(ser):
                ser.timeout = 3
                d = ser.read(n)
                if len(d) == n:
                    return d
        time.sleep(0.05)
        ser.reset_input_buffer()
    return None


def go(ser, addr):
    if not cmd(ser, 0x21):
        return False
    return send_addr(ser, addr)


data = open(BINF, 'rb').read()
if len(data) % 4:
    data += b'\xff' * (4 - len(data) % 4)

ser = serial.Serial(PORT, 115200, parity=serial.PARITY_EVEN, stopbits=1,
                    bytesize=8, timeout=2)
print("Conectando al bootloader en %s ..." % PORT)
if not connect(ser):
    print(">>> NO conecta. Entra en bootloader (Bootload+reset) y reintenta.")
    sys.exit(1)

print("Conectado. Mass erase...")
if not erase_all(ser):
    print(">>> erase FALLO")
    sys.exit(1)

# Bloques de 128 B en vez de 256: la mitad de energia por escritura, o sea
# medio pico de corriente en el rail que alimenta la linea serie. Tarda algo
# mas, pero llegar al final a la primera sale mucho mas barato que reintentar
# los 209 KB.
BLK = 128
PAUSA = 0.003   # deja respirar al rail entre bloques

print("Grabando %d bytes en 0x%08X (bloques de %d B) ..." % (len(data), ADDR, BLK))
off = 0
reintentos = 0
while off < len(data):
    chunk = data[off:off + BLK]
    if not write_mem(ser, ADDR + off, chunk):
        print("\n>>> write FALLO en offset 0x%X (irrecuperable tras 8 intentos"
              " y resincronizacion)" % off)
        sys.exit(1)
    off += len(chunk)
    time.sleep(PAUSA)
    print("  %d/%d bytes" % (off, len(data)), end='\r')

print("\nVerificando (releyendo el flash)...")
off = 0
bad = 0
while off < len(data):
    n = min(256, len(data) - off)
    got = read_mem(ser, ADDR + off, n)
    if got is None:
        print("\n>>> no se pudo LEER en offset 0x%X" % off)
        sys.exit(1)
    if got != data[off:off + n]:
        bad += 1
        print("\n>>> VERIFICACION FALLO en offset 0x%X" % off)
        sys.exit(1)
    off += n
    print("  %d/%d bytes" % (off, len(data)), end='\r')

print("\nVerificado OK. Ejecutando app (Go 0x%08X)..." % ADDR)
go(ser, ADDR)
ser.close()
print(">>> FLASHEO COMPLETO Y VERIFICADO")
