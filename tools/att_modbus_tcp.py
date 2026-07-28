#!/usr/bin/env python3
"""Cliente Modbus TCP minimo para la ATT (unit id 1, puerto 502)."""
import socket, struct, sys

HOST = "192.168.50.186"
PORT = 502
UNIT = 1


def tx(sock, pdu, tid=1):
    frame = struct.pack(">HHHB", tid, 0, len(pdu) + 1, UNIT) + pdu
    sock.sendall(frame)
    hdr = sock.recv(7)
    if len(hdr) < 7:
        raise IOError("respuesta corta")
    ln = struct.unpack(">H", hdr[4:6])[0]
    body = b""
    while len(body) < ln - 1:
        body += sock.recv(ln - 1 - len(body))
    if body[0] & 0x80:
        raise IOError("excepcion Modbus 0x%02x en func 0x%02x" % (body[1], body[0] & 0x7F))
    return body


def main():
    op = sys.argv[1]
    sock = socket.create_connection((HOST, PORT), timeout=5)
    try:
        if op == "rd":
            addr, qty = int(sys.argv[2]), int(sys.argv[3])
            body = tx(sock, struct.pack(">BHH", 3, addr, qty))
            n = body[1]
            vals = struct.unpack(">%dH" % (n // 2), body[2:2 + n])
            for i, v in enumerate(vals):
                print("hr%-3d = %5d  (0x%04x)" % (addr + i, v, v))
        elif op == "wr":
            addr, val = int(sys.argv[2]), int(sys.argv[3])
            body = tx(sock, struct.pack(">BHH", 6, addr, val))
            a, v = struct.unpack(">HH", body[1:5])
            print("escrito hr%d = %d" % (a, v))
    finally:
        sock.close()


main()
