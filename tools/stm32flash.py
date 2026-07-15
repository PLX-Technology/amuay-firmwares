import serial, sys, time

PORT = 'COM6'
BINF = sys.argv[1] if len(sys.argv) > 1 else 'att_blinky.bin'
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

def write_mem(ser, addr, data):
    for _ in range(4):
        if cmd(ser, 0x31):
            a = addr.to_bytes(4, 'big')
            ser.write(a + bytes([a[0] ^ a[1] ^ a[2] ^ a[3]]))
            if ack(ser):
                n = len(data)
                ck = (n - 1)
                for b in data:
                    ck ^= b
                ser.write(bytes([n - 1]) + data + bytes([ck & 0xFF]))
                if ack(ser, tmo=3):
                    return True
        time.sleep(0.05)
        ser.reset_input_buffer()
    return False

def go(ser, addr):
    if not cmd(ser, 0x21):
        return False
    a = addr.to_bytes(4, 'big')
    ser.write(a + bytes([a[0] ^ a[1] ^ a[2] ^ a[3]]))
    return ack(ser)

data = open(BINF, 'rb').read()
if len(data) % 4:
    data += b'\xff' * (4 - len(data) % 4)
ser = serial.Serial(PORT, 115200, parity=serial.PARITY_EVEN, stopbits=1, bytesize=8, timeout=2)
print("Conectando al bootloader en %s ..." % PORT)
if not connect(ser):
    print(">>> NO conecta. Entra en bootloader (Bootload+reset) y reintenta."); sys.exit(1)
print("Conectado. Mass erase...")
if not erase_all(ser):
    print(">>> erase FALLO"); sys.exit(1)
print("Grabando %d bytes en 0x%08X ..." % (len(data), ADDR))
off = 0
while off < len(data):
    chunk = data[off:off + 256]
    if not write_mem(ser, ADDR + off, chunk):
        print("\n>>> write FALLO en offset 0x%X" % off); sys.exit(1)
    off += len(chunk)
    print("  %d/%d bytes" % (off, len(data)), end='\r')
print("\nGrabado OK. Ejecutando app (Go 0x%08X)..." % ADDR)
go(ser, ADDR)
ser.close()
print(">>> FLASHEO COMPLETO")
