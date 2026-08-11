"""Verifica el flash de la ATT contra un binario, SIN borrar ni reescribir.

    python verificar_att.py <bin> <COMx>

Por que existe: stm32flash.py escribe y verifica en la misma pasada, asi que un
fallo de LECTURA al verificar obliga a repetir los 209 KB enteros aunque la
escritura hubiera ido bien. Esto separa las dos cosas.

Y arregla la asimetria que provoca ese fallo: read_mem() del flasher reintenta
cuatro veces pero NUNCA resincroniza, mientras que write_mem() si lo hace a
partir del tercer intento. Cuando la linea pierde un byte el bootloader sigue
vivo pero descolocado, y sin volver a sincronizar los cuatro reintentos fallan
en cadena -- que es exactamente como murio la verificacion en 0x1F00.
"""
import serial, sys, time

BINF = sys.argv[1]
PORT = sys.argv[2]
# Direccion a comparar. Por defecto el principio del flash; se puede dar otra
# para leer UNA particion suelta, p.ej. 0x080f0000 (slot1) y ver que dejo ahi
# una subida OTA por SMP frente a lo que deberia haber.
ADDR = int(sys.argv[3], 0) if len(sys.argv) > 3 else 0x08000000


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
        if ser.read(1) in (b'\x79', b'\x1f'):
            return True
        time.sleep(0.3)
    return False


def send_addr(ser, addr):
    a = addr.to_bytes(4, 'big')
    ser.write(a + bytes([a[0] ^ a[1] ^ a[2] ^ a[3]]))
    return ack(ser)


def read_mem(ser, addr, n, resyncs):
    """Read Memory (0x11), con la MISMA estrategia que write_mem: espera
    creciente y resincronizacion a partir del tercer intento."""
    for intento in range(8):
        if cmd(ser, 0x11) and send_addr(ser, addr):
            ser.write(bytes([n - 1, (n - 1) ^ 0xFF]))
            if ack(ser):
                ser.timeout = 3
                d = ser.read(n)
                if len(d) == n:
                    return d
        ser.reset_input_buffer()
        time.sleep(0.05 * (intento + 1))
        if intento >= 2:
            resyncs.append(addr)
            connect(ser)
    return None


data = open(BINF, 'rb').read()
if len(data) % 4:
    data += b'\xff' * (4 - len(data) % 4)

ser = serial.Serial(PORT, 115200, parity=serial.PARITY_EVEN, stopbits=1,
                    bytesize=8, timeout=2)
print("Conectando al bootloader en %s ..." % PORT)
if not connect(ser):
    print(">>> NO conecta. La placa tiene que seguir en bootloader.")
    sys.exit(1)
print("Conectado. Comparando %d bytes contra %s ..." % (len(data), BINF))

off, resyncs, difs = 0, [], []
while off < len(data):
    n = min(256, len(data) - off)
    got = read_mem(ser, ADDR + off, n, resyncs)
    if got is None:
        print("\n>>> no se pudo LEER en offset 0x%X ni tras 8 intentos "
              "con resincronizacion" % off)
        sys.exit(1)
    if got != data[off:off + n]:
        difs.append(off)
        if len(difs) > 8:
            print("\n>>> demasiadas diferencias, el flash NO coincide")
            sys.exit(2)
    off += n
    if off % 16384 == 0:
        print("  %d/%d bytes" % (off, len(data)), end='\r')

print("\n--- resultado ---")
print("  resincronizaciones necesarias: %d %s"
      % (len(resyncs), ["", "(la linea serie es inestable)"][bool(resyncs)]))
if difs:
    print("  BLOQUES DISTINTOS: %s" % ", ".join("0x%X" % d for d in difs))
    print(">>> el flash NO coincide con el binario: hay que reescribir")
    sys.exit(2)
print(">>> FLASH CORRECTO: coincide byte a byte con %s" % BINF)
