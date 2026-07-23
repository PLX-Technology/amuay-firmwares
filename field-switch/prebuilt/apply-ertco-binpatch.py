#!/usr/bin/env python3
"""Parchea el bucle infinito de espera de ERTCO (MXC_SYS_Clock_Timeout) en la
imagen del field switch: beq.n .-4 -> nop. Entrada: .bin (imagen sin header,
p.ej. extraida de mfs_pullup.sbin como sbin[256:-64]). Luego firmar con:
  sign_app -c MAX32690 ca=out.bin sca=out.sbin header=yes rom_version=010203ff \
           jump_address=<__start>
y si no arranca (sign_app es intermitente), RE-FIRMAR y reintentar."""
import sys, re
d = bytearray(open(sys.argv[1], "rb").read())
pat = bytes.fromhex("4ff08042" "9368" "2342" "fcd0")  # mov.w r2,#0x40000000; ldr; tst; beq
i = [m.start() for m in re.finditer(re.escape(pat), d)]
assert len(i) == 1, "patron no unico: %r" % i
off = i[0] + 8  # el beq (fcd0)
assert d[off:off+2] == bytes.fromhex("fcd0")
d[off:off+2] = bytes.fromhex("00bf")  # nop
open(sys.argv[2], "wb").write(d)
print("parcheado offset 0x%x: beq -> nop" % off)
