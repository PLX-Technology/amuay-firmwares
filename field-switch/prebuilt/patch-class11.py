import re, sys

SBIN = r"C:/Users/joseh/repos/pacific-firmware/field-switch/prebuilt/mfs_fix.sbin"
OUT_BIN = r"C:/Users/joseh/AppData/Local/Temp/claude/C--Users-joseh-OneDrive-Documentos-Proyectos2026-Planner/6d3d5729-670f-44fc-bae7-c215da986bee/scratchpad/mfs_class11.bin"

sbin = open(SBIN, "rb").read()
print("sbin len:", len(sbin))
img = bytearray(sbin[256:-64])
print("img len:", len(img))
print("ertco @0xae48:", img[0xae48:0xae4a].hex(), "(esperado 00bf)")

pat = bytes.fromhex("01000000fa000000")  # power_class=1 (padded) + hs_resistor=250
hits = [m.start() for m in re.finditer(re.escape(pat), bytes(img))]
print("hits:", [hex(h) for h in hits])
for a, b in zip(hits, hits[1:]):
    print("stride:", b - a)

if len(hits) == 4 and all(b - a == 24 for a, b in zip(hits, hits[1:])):
    # sanity: 16 bytes antes de cada hit deben ser los gpio specs (mismo device ptr en los 4)
    ptrs = {bytes(img[h-16:h-12]).hex() for h in hits}
    print("gpio dev ptrs:", ptrs)
    for h in hits:
        img[h] = 0x03  # LTC4296_PSE_SCCP_CLASS_11
    open(OUT_BIN, "wb").write(img)
    print("PATCHED ->", OUT_BIN)
else:
    print("ABORT: patron no coincide con 4 entradas stride 24")
    sys.exit(1)
