#!/usr/bin/env python3
"""Instrumentacion TEMPORAL del arranque del MFS. No es codigo de produccion.

Inserta marcas por printk() -- no por LOG_*, para que sobrevivan a cualquier
nivel de registro y no dependan del subsistema de logs, que es justo lo que
esta bajo sospecha.

Sintoma que se persigue: la placa imprime el cartel de Zephyr, se queda muda y
se reinicia a los 19,7 s clavados. El periodo es identico en binarios distintos,
asi que el reinicio parece externo (watchdog o hardware) y lo que hay que
localizar es DONDE se cuelga.

Uso: python3 marcas_arranque.py <main.c>
"""
import io
import sys

P = sys.argv[1]
s = io.open(P, encoding='utf-8', errors='replace', newline=None).read()

if 'MFS_MK' in s:
    print('YA instrumentado')
    sys.exit(0)


def sub1(old, new, que):
    global s
    n = s.count(old)
    assert n == 1, 'ancla "%s": esperaba 1 aparicion, hay %d' % (que, n)
    s = s.replace(old, new, 1)


CABECERA = '''/* --- INSTRUMENTACION TEMPORAL DE ARRANQUE (quitar antes de produccion) --- */
#define MFS_MK(x) printk("[MK] " x " @%lld\\n", k_uptime_get())
static int mfs_mk_pk2(void)  { MFS_MK("PRE_KERNEL_2"); return 0; }
static int mfs_mk_post(void) { MFS_MK("POST_KERNEL");  return 0; }
static int mfs_mk_app(void)  { MFS_MK("APPLICATION");  return 0; }
SYS_INIT(mfs_mk_pk2,  PRE_KERNEL_2, 99);
SYS_INIT(mfs_mk_post, POST_KERNEL,  99);
SYS_INIT(mfs_mk_app,  APPLICATION,  99);

int main(void)
{
\tint32_t ret;'''

sub1('int main(void)\n{\n\tint32_t ret;', CABECERA, 'cabecera')

MARCAS = [
    ('\tret = adin6310_enable_pse(ltc4296_dev, switch_op);', 'antes de enable_pse'),
    ('\tret = SES_Init();', 'antes de SES_Init'),
    ('\tret = SES_AddHwInterface(NULL, &comm_callbacks, &iface);', 'antes de AddHwInterface'),
    ('\tk_thread_start(spi_read_tid);', 'antes de arrancar el lector SPI'),
    ('\tret = SES_AddDevice(iface, mac_addr, &dev_id);', 'antes de AddDevice'),
    ('\tret = SES_MX_InitializePorts(dev_id, 6, initializePorts_p);', 'antes de InitializePorts'),
]

for ancla, texto in MARCAS:
    sub1(ancla, '\tMFS_MK("%s");\n%s' % (texto, ancla), texto)

if '#include <zephyr/init.h>' not in s:
    i = s.index('#include')
    s = s[:i] + '#include <zephyr/init.h>\n' + s[i:]

io.open(P, 'w', encoding='utf-8', newline='\n').write(s)
print('OK: %d marcas insertadas (3 niveles de init + %d en main)'
      % (3 + len(MARCAS), len(MARCAS)))
