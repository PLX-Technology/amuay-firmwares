#!/usr/bin/env python3
"""Rellena una imagen firmada hasta pagina completa de flash del MAX32690.

⚠️ SIN ESTO, EL GRABADO POR SERIE FALLA SIEMPRE EN EL 99 %.

El flash del MAX32690 se programa por paginas de 16 KB. Si el ultimo bloque
que manda la sesion SCP cae a mitad de pagina, el ROM responde

    module=11, code=6  ->  ERR_FATAL_ERROR : Critical error

justo en el ultimo `write_mem`. Reproducible: 4 de 4 con la imagen sin
rellenar, 100 % con ella rellenada.

LA PISTA: openocd, grabando la MISMA imagen por SWD y con exito, decia
"wrote 638976 bytes" para un fichero de 626812. Rellena hasta pagina completa
porque asi se programa el flash. Esto hace lo mismo.

El relleno va DESPUES de la firma y no la invalida: la cabecera declara la
longitud real, asi que el ROM ignora el sobrante. Se rellena con 0xFF, que es
el valor del flash borrado.

Uso: python3 pad_a_pagina.py <entrada.sbin> [salida.sbin]
"""
import os
import sys

PAGINA = 16384          # 16 KB, tamano de pagina del flash del MAX32690

src = sys.argv[1]
dst = sys.argv[2] if len(sys.argv) > 2 else src.replace('.sbin', '_pad.sbin')

datos = open(src, 'rb').read()
total = ((len(datos) + PAGINA - 1) // PAGINA) * PAGINA
relleno = total - len(datos)

if relleno == 0:
    print('ya esta alineada a pagina: %d bytes (%d paginas)'
          % (len(datos), total // PAGINA))
else:
    open(dst, 'wb').write(datos + b'\xff' * relleno)
    print('%s: %d bytes' % (os.path.basename(src), len(datos)))
    print('%s: %d bytes  (+%d de relleno, %d paginas de 16K)'
          % (os.path.basename(dst), total, relleno, total // PAGINA))
