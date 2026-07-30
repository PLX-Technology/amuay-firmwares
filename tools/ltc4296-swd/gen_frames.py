#!/usr/bin/env python3
"""Genera los frames SPI de 5 bytes del LTC4296 (PEC CRC8 seed 0x41).

Se autovalida contra los frames ya conocidos de vout_p3.tcl / gadc_vout.tcl
antes de emitir nada: si el algoritmo no reproduce esos, aborta.
"""

def pec(data, seed=0x41):
    p = seed
    for bit in range(7, -1, -1):
        din = (data >> bit) & 1
        in0 = din ^ ((p >> 7) & 1)
        in1 = in0 ^ (p & 1)
        in2 = in0 ^ ((p >> 1) & 1)
        p = ((p << 1) & 0xFF) & ~0x07
        p |= in0 | (in1 << 1) | (in2 << 2)
    return p & 0xFF

def wr(reg, val):
    b0 = (reg << 1) | 0
    return [b0, pec(b0), (val >> 8) & 0xFF, val & 0xFF,
            pec(val & 0xFF, pec((val >> 8) & 0xFF))]

def rd(reg):
    b0 = (reg << 1) | 1
    return [b0, pec(b0), 0, 0, 0]

def fmt(f):
    return ' '.join('0x%02x' % b for b in f)

# --- autovalidacion contra los scripts existentes -----------------------
P3CFG1, P3CFG0, P3ST = 0x44, 0x43, 0x42
GADCCFG, GADCDAT     = 0x0A, 0x0B
KNOWN = [
    (wr(P3CFG1, 0x0108),        [0x88, 0x71, 0x01, 0x08, 0x63], 'P3CFG1=0x0108'),
    (wr(P3CFG0, 0x2041),        [0x86, 0x5b, 0x20, 0x41, 0x20], 'P3CFG0=0x2041'),
    (wr(GADCCFG, 0x004A),       [0x14, 0xac, 0x00, 0x4a, 0xbf], 'GADCCFG vout p3'),
    (wr(GADCCFG, 0x0048),       [0x14, 0xac, 0x00, 0x48, 0xb1], 'GADCCFG vout p2'),
    (rd(GADCDAT),               [0x17, 0xa5, 0, 0, 0],          'read GADCDAT'),
    (rd(P3ST),                  [0x85, 0x52, 0, 0, 0],          'read P3ST'),
    (rd(0x32),                  [0x65, 0xfc, 0, 0, 0],          'read P2ST'),
    (wr(P3CFG0, 0x0000),        [0x86, 0x5b, 0x00, 0x00, 0x4e], 'P3CFG0=0'),
]
for got, want, name in KNOWN:
    assert got == want, 'PEC MAL en %s: %s != %s' % (name, fmt(got), fmt(want))
print('autovalidacion OK (8/8 frames conocidos reproducidos)\n')

# --- frames para el MPS -------------------------------------------------
# P0 = slot 1 (PSM + PDM del MFS)   P1 = slot 2 (vacio, control A/B)
def port(p):
    return dict(EV=(p+1)*0x10, ST=(p+1)*0x10+2,
                CFG0=(p+1)*0x10+3, CFG1=(p+1)*0x10+4)

VOUT_SEL = {0: 0x0044, 1: 0x0046, 2: 0x0048, 3: 0x004A, 4: 0x004C}

for p in (0, 1):
    r = port(p)
    print('--- puerto %d (slot %d) ---' % (p, p + 1))
    print('  prebias   PxCFG1=0x0108 : %s' % fmt(wr(r['CFG1'], 0x0108)))
    print('  classif   PxCFG0=0x2041 : %s' % fmt(wr(r['CFG0'], 0x2041)))
    print('  gadc vout GADCCFG=0x%04x: %s' % (VOUT_SEL[p], fmt(wr(GADCCFG, VOUT_SEL[p]))))
    print('  read PxST               : %s' % fmt(rd(r['ST'])))
    print('  read PxEV               : %s' % fmt(rd(r['EV'])))
    print('  DISABLE   PxCFG0=0x0000 : %s' % fmt(wr(r['CFG0'], 0x0000)))
    print()

print('--- globales ---')
print('  read GCFG  (0x09) : %s' % fmt(rd(0x09)))
print('  read GSTAT (0x01) : %s' % fmt(rd(0x01)))
print('  read GFLTEV(0x02) : %s' % fmt(rd(0x02)))
print('  read GADCDAT      : %s' % fmt(rd(GADCDAT)))
print('  GADCCFG off       : %s' % fmt(wr(GADCCFG, 0x0000)))
