# Field switch (MAX32690 + ADIN6310) — arranque y "no linkean los SPE"

> Bitácora de depuración (2026-07-22). El síntoma final era **"los puertos SPE no
> linkean"**, pero la causa raíz **no** estaba en los PHY ni en el cableado: el
> firmware del field switch **se colgaba en el arranque** y nunca llegaba a
> configurar el switch ni los ADIN1100. Resuelto.

## TL;DR

Una placa que "no auto-arranca" / "no linkea" puede estar en **dos estados muy
distintos**. Se distinguen leyendo el **PC por SWD** tras un POR frío:

| PC tras POR | Estado real | Causa | Solución |
|---|---|---|---|
| `0x1000af46` (o cerca, en flash `0x1000xxxx` y **clavado**) | Arrancó (secure-boot OK) pero **colgado en init** | **Cuelgue en ERTCO** (ver abajo) | Flashear el firmware con el fix (binpatch) + POR |
| `0x1000xxxx` **moviéndose** (p.ej. `0xb5xx`/`0x7axx`) | **Corriendo normal** | — | OK, ya funciona |
| `0x0000xxxx` rebotando (`0x886`,`0x2662`,`0x6f84`) | En el **bootloader ROM**, no saltó a la app | Imagen no valida: **firma mala** (ver sign_app) o **CRK ausente** | Re-firmar/reintentar; si persiste, comprobar CRK en `0x10801000` |

**Comando para leer el PC** (Pico SWD, serial del field switch):
```
openocd -c "adapter driver cmsis-dap" -c "adapter serial <SERIAL>" \
  -c "adapter speed 1000" -f target/max32690.cfg \
  -c "reset_config none" -c "init" -c "halt" -c "reg pc" -c "resume" -c "shutdown"
```

---

## Causa raíz #3 — reinicio periódico de **19,7 s** con la placa a 24 V

2026-08-04, banco. Síntoma: la placa imprime el cartel de Zephyr, **se queda
muda** y se reinicia. Ocho arranques seguidos, intervalos de **19,7 s clavados**.

### ⚠️ Lo que descarta que sea culpa del firmware

**El mismo ciclo de 19,7 s corta las sesiones de grabado por serie**, que mueren
siempre en el **11-12 %** — que a 0,55 s/paquete son justo ~20 s de sesión.
Durante un grabado SCP **la aplicación no corre**: manda el bootloader del ROM.
Si el reinicio ocurre igual, **no lo provoca el código de la aplicación**.

Corolario práctico: los grabados que terminan al 100 % son los que enganchan la
ventana del ROM **en el primer arranque tras el POR**, antes de que la
aplicación llegue a correr. Los que enganchan una vuelta más tarde mueren al
11-12 %. Por eso el éxito parece aleatorio y los fallos caen siempre en el mismo
porcentaje: **no es transporte ni suerte, es el ciclo de reinicio**.

### Sospechoso: el LTC4296 con la placa fuera de rango

El LTC4296 es un **chip aparte con su propia lógica** y **no se reinicia cuando
lo hace el micro**. Con la placa a **24 V** —fuera de su rango de clase 13, que
es 50-58 V— queda intentando periódicamente y cada intento puede hundir la
alimentación de banco lo justo para reiniciar el MAX32690.

**Comprobación de diez segundos, sin grabar nada:** mirar el amperímetro de la
fuente. Un pico de corriente cada ~20 s confirma la hipótesis.

**La prueba que falta: alimentar la placa a 50 V.** Es la condición de diseño y
la única que no se ha probado.

### Descartado por el camino (para no repetirlo)

- **No es `CONFIG_LOG`.** Con la configuración de campo (`LOG=y`, nivel 4,
  inmediato) se reinicia igual. Llegué a acusar al registro y era falso.
- **No es la configuración.** `prj.conf.bak-prod` y `prj.conf.bak-mio` son el
  **mismo fichero** (793 B los dos); creí que diferían por leer mal el `ls`.
- **No es el ERTCO** (causa raíz #1): el bucle ya está acotado en
  `sys_me18.c:331` y el patrón del binpatch **no aparece** en el binario.
- **No es una llamada con efectos colaterales dentro de una macro `LOG_*`**
  (el clásico al apagar el registro): no hay ninguna, ni en `main.c` ni en el
  driver del LTC4296.
- **No hay watchdog software**: `CONFIG_TASK_WDT` no está activado.

---

## Causa raíz #1 — cuelgue en ERTCO (32 kHz) en el HAL de ADI

El board devicetree (`boards/adi/mfs06/mfs06_max32690_m4.dts`) habilita el ERTCO:

```dts
/* ERTCO requires for RTC */
&clk_ertco { status = "okay"; };
```

En el arranque, el driver `clock_control` de Zephyr habilita ese reloj y **espera
a que quede listo** con esta función del HAL de ADI
(`modules/hal/adi/MAX/Libraries/PeriphDrivers/Source/SYS/sys_me18.c`):

```c
int MXC_SYS_Clock_Timeout(uint32_t ready) {
    MXC_DelayAsync(MXC_SYS_CLOCK_TIMEOUT, NULL);
    /* TODO: Timeout on clock switch, use this for untrimmed parts. */
    while (!(MXC_GCR->clkctrl & ready)) {}   // <-- BUCLE INFINITO, el timeout es un TODO
    return E_NO_ERROR;
}
```

En estas placas **el cristal de 32.768 kHz no oscila**, así que `ERTCO_RDY`
(`clkctrl` bit 25) nunca se pone a 1 → **bucle infinito eterno**. El firmware
**nunca** llega a `main` ni a configurar el switch → ningún PHY levanta → **los
SPE no pueden linkear**.

Verificado en vivo por SWD: PC clavado en `0x1000af46` (`tst r3,r4; beq .-4`),
`r4 = 0x02000000 = ERTCO_RDY`, `clkctrl` con bits 24 (ERFO) y 25 (ERTCO) en 0.

### Fix aplicado (binpatch de 2 bytes sobre la imagen buena)

Se parchea el `beq` del bucle a `nop` para que **no espere** (los relojes
internos IPO/ISO/IBRO ya están listos; el ERTCO no hace falta, el firmware no usa
RTC). En la imagen `mfs_pullup` el bucle está en **flash `0x1000af48`** =
**offset `0xae48`** dentro del `.bin` (imagen sin header):

```
offset 0xae48:  fc d0   (beq.n .-4)   ->   00 bf   (nop)
```
Patrón de anclaje (único): `4f f0 80 42  93 68  23 42  fc d0`
(`mov.w r2,#0x40000000; ldr r3,[r2,#8]; tst r3,r4; beq .-4`).

### Fix "correcto" (parche de fuente en el HAL, pendiente de validar)

Alternativa limpia al binpatch — acotar el bucle en `sys_me18.c`:
```c
    /* FIX: bucle acotado, no colgar si el reloj (ERTCO/cristal 32k) no arranca */
    { uint32_t __to = 0x200000;
      while (!(MXC_GCR->clkctrl & ready)) { if (--__to == 0) break; } }
```
Rebuild con `CONFIG_FLASH_LOAD_OFFSET=0x100` (¡ver abajo!). **NO** deshabilitar
`clk_ertco` en el overlay: quitar el nodo provoca un **fault temprano** →
reset → ROM (probado). Hay que dejar el nodo *okay* y arreglar el HAL.

---

## Causa raíz #2 (trampa que confundió todo) — `sign_app` firma INTERMITENTE

`sign_app.exe` (MaximSDK SBT) genera a veces una **firma ECDSA inválida**. Una
imagen correctamente construida y flasheada se queda en el ROM (`0x0000xxxx`)
como si la firma o la CRK estuvieran mal, aunque no lo estén.

**Regla:** si un `.sbin` bien hecho no arranca, **re-firmar y reintentar** antes
de sospechar del binario. Confirmar por el PC tras POR (debe pasar a `0x1000xxxx`).

Receta de firma (recordatorio; ver también memoria del proyecto):
```
sign_app.exe -c MAX32690 ca=fw.bin sca=fw.sbin header=yes \
  rom_version=010203ff jump_address=<__start del zephyr.map, sin 0x>
```
Requisitos de build que si faltan producen firmware que **nunca** arranca:
`CONFIG_FLASH_LOAD_OFFSET=0x100`, `CONFIG_SRAM_VECTOR_TABLE=n`, `rom_version=010203ff`.

> ⚠️ **OJO con el build tree:** en la TPU existían `build_mfs` y `build_mfs_nr`.
> `build_mfs_nr` tenía `FLASH_LOAD_OFFSET=0` (¡mal!). Además un `ninja` a secas
> **reconfigura y pierde** el `-DCONFIG_FLASH_LOAD_OFFSET=0x100` pasado por línea
> de comandos (el `prj.conf` del *sample* en la TPU lo tiene comentado). Este
> repo ya lo trae en `prj.conf`, así que **construir desde este repo** evita el
> problema. Build pristine correcto:
> ```
> west build -p always -b mfs06/max32690/m4 <sample> -d <dir> -- \
>   -DLIB_ADIN6310_PATH=/home/tpu01/ADIN6310SWDR-Rel5.1.0
> ```

---

## Investigación del bloqueo "builds frescas no arrancan" (2026-07-23)

Estado: **parcialmente resuelto, con un factor residual abierto.**

### Factor 1 (CONFIRMADO y corregido): `FLASH_LOAD_OFFSET=0`

Análisis binario byte a byte de 4 builds: el `prj.conf` del sample tenía
`# CONFIG_FLASH_LOAD_OFFSET is not set`, así que cada build pristine linkeaba en
`0x10000000` (offset 0). Con el header de secure boot (256 B) el cuerpo baja a
`0x10000100`, dejando **cada dirección 0x100 por debajo** de su sitio físico →
el ROM salta 0x100 antes de `__start` → basura → ROM.

- Imágenes que ARRANCAN (mfs_pullup y derivadas): `.bin` con **256 bytes de
  ceros al inicio**, vectores en file `0x100`, `_vector_table=0x10000200`.
- Imágenes que NO arrancaban (build_mfs, build_shortcheck): **0 bytes de
  relleno**, vectores en file `0x0`, `_vector_table=0x10000000`.

**Fix aplicado:** `prj.conf` línea 30 → `CONFIG_FLASH_LOAD_OFFSET=0x100`. Un
rebuild pristine ya sale con `_vector_table=0x10000200` y el `.bin` con los 256
ceros. Verificado.

### Factor 2 (ABIERTO): sigue sin arrancar con offset correcto

⚠️ **El offset era necesario pero NO suficiente.** Probado en vivo:

| Imagen | offset | estructura | POR |
|---|---|---|---|
| mfs_class11 (cuerpo mfs_pullup, re-firmada hoy) | 0x100 | 256-ceros ✓ | **arranca** ✓ |
| mfs_short (build fresco, offset corregido, firmada hoy) | 0x100 | 256-ceros ✓, vectores/jump correctos | **ROM** ✗ |
| mfs_ms (otro build fresco, offset 0x100) | 0x100 | 256-ceros ✓ | **ROM** ✗ |

Descartado con datos:
- **Placa sana**: mfs_class11 arranca una y otra vez (PC `0x1000xxxx`).
- **No es fault de runtime**: el PC tras POR es ROM (`0x0000xxxx`) con HFSR/CFSR
  = 0 → rechazo del secure boot, no arranque-y-cuelgue.
- **No es `sign_app` intermitente**: firma **determinista** (3 firmas del mismo
  `.bin` = byte-idénticas). Re-firmar no cambia nada.
- **Header estructuralmente idéntico** entre la que arranca y la que no
  (mfs_class11 vs mfs_short): `load=0x10000000`, `imglen=cuerpo+224`, vectores
  y SP/Reset correctos. Solo difieren `imglen`, `jump` y los 64 B de firma
  (todo esperable). Firmas bien formadas (sin ceros a la cabeza en r/s).

### Factor 3 (PROBADO cripto): la firma es VÁLIDA — NO es el secure boot

Verificación criptográfica offline (código fuente de `sign_app` + `cryptography`):
- Esquema: **ECDSA P-256 + SHA-256**, firma `r||s` big-endian (64 B), sobre el
  rango `data[0 : len-64]` (header + payload, todo menos la firma).
- Clave: `maximtestcrk` (`devices/MAX32690/keys/maximtestcrk.key`):
  X=`a823c8857948dc68…1dcf0142`, Y=`3be124619cbbeb51…f2db8efe`.
- **Resultado: los 14 `.sbin` verifican TRUE, incluida `mfs_short.sbin` (la que
  no arranca) y todos los builds frescos.** La firma de los builds frescos es
  criptográficamente válida contra la misma clave que valida las que arrancan.
- `imglen` correcto en ambos (`=filesize+224`). Header sano.

⇒ **El ROM ACEPTA la imagen fresca y salta a `jump_address`.** El "no arranca"
**NO es rechazo del secure boot** — es un **cuelgue/fault DESPUÉS del salto**,
en el arranque de la app. La nota de memoria "sign_app firma intermitente"
queda **DESMENTIDA** (firma determinista y válida; re-firmar no cambia nada).

### ✅ RESUELTO (2026-07-23): el bloqueo NUNCA fue de arranque — era código tóxico

**Los builds frescos SÍ arrancan.** Todo lo de "fresh builds no arrancan" era un
diagnóstico equivocado. Cadena de descubrimientos:

1. Offset corregido (`prj.conf` → `0x100`) — necesario.
2. Firma probada VÁLIDA (cripto) — secure boot descartado.
3. **Firmware mínimo "blink+spin" compilado fresco → ARRANCA** (PC flash,
   moviéndose). Prueba definitiva: secure boot, offset, firma y arranque base
   están PERFECTOS para builds frescos.
4. **Bisección con `while(1)` spin + lectura de PC** (fiable; la vía `srst`+SWD
   es engañosa, no replica secure boot): el spin tras `SES_MX_InitializePorts`
   se alcanza → toda la init del switch (LTC + SES + 6 puertos) funciona en
   fresco. El fault estaba DESPUÉS.
5. **Root cause: el bloque de sondeo RJ45/ADIN1300 del `macPort5`** en `main.c`
   (tras "Configuration done"): reconfigura `macPort5` como `SES_phyADIN1300`,
   hace `rj_mmd_rd/wr`, fuerza RMII, reinicia autoneg... sobre un **PHY RJ45 que
   NO EXISTE en el field switch** (es todo SPE; el RJ45 es del power switch). Su
   propio comentario avisaba: *"dejarlo mal-configurado CRASHEA la app"*.
   Crashea → hard-fault → reset → ROM (por eso parecía "no arranca").

**FIX:** eliminar ese bloque (inútil y dañino aquí). Firmware definitivo:
`prebuilt/mfs_clean_class11.sbin` = init limpia del switch SPE + overlay
**Clase 11** (seguro, sin entrega ciega) + feature de detección de cortos
(`mfs-short-detect/`). **Compila, arranca y la feature corre** (probado:
`g_short_mask=0`, sondeo 5178 mV/puerto). Fuente de referencia:
`mfs-short-detect/main.c.reference`.

**Lección:** un build fresco del mfs se compila, firma (jump = su `__start`) y
flashea con la receta de siempre y ARRANCA. El firmware es libremente
modificable como el del power switch. Para depurar arranque: firmware mínimo +
bisección con spin+PC, NO asumir secure boot ni perseguir `srst`.

---

## ¿Era la CRK el problema? (corrección de una hipótesis previa)

**No, para las placas que YA traen la CRK.** Se creyó que el "no auto-arranque"
era falta de CRK (Customer Root Key en OTP `0x10801000`). Falso para estas: el
power switch y los field switches con la CRK de fábrica **sí** pasan el
secure-boot y saltan a la app; lo que fallaba era el **cuelgue en ERTCO** una vez
dentro (PC en `0x1000af46`, no en el ROM).

La CRK **sí** importa solo para una placa **genuinamente virgen** (`0x10801000`
todo `0xff`): esa se queda en el ROM (`0x0000xxxx`) y necesita aprovisionamiento
por `send_scp writemaximcrk` sobre una UART del bootloader (no por SWD; `send_scp`
no tiene interfaz SWD). Distinguir por el PC: **flash `0x1000xxxx` = tiene CRK**
(problema ERTCO); **ROM `0x0000xxxx` = comprobar CRK**.

---

## Procedimiento para aprovisionar/arreglar un field switch nuevo

1. Conectar el Pico SWD. Leer el PC tras POR (comando arriba) y `0x10801000`
   (CRK) tras desbloquear FLC:
   `mww 0x40029040 0x1234; 0x3a7f5ca3; 0xa1e34f20; 0x9608b2c1; mdw 0x10801000 8`.
2. Si CRK presente (datos, no `0xff`): **flashear el firmware con el fix**:
   ```
   openocd ... -c "reset halt" -c "flash write_image erase mfs_fix.sbin 0x10000000" \
       -c "verify_image mfs_fix.sbin 0x10000000" -c "shutdown"
   ```
   Luego **POR frío** (quitar y devolver alimentación; un reset caliente no
   dispara el secure-boot). Verificar PC en `0x1000xxxx` moviéndose. Si quedó en
   ROM → **re-firmar** el .sbin y repetir (sign_app intermitente).
3. Si CRK ausente (`0xff`): **aprovisionar por el propio Pico** (VERIFICADO
   2026-07-23 — ver procedimiento abajo). NO hace falta USB-UART externo.

## Aprovisionar la CRK con SOLO el Pico (procedimiento VERIFICADO)

El conector **uC SWD (J11)** del field switch lleva `LPUART_TX`/`LPUART_RX`
(pines 6/8) además del SWD — la **misma UART (LPUART0B) que usa el bootloader
SCP del ROM**, puenteada por la CDC-UART del MAX32625PICO (el COM del Pico).
Mismo esquema que el power switch. ⚠️ Usar **`tools/sscp/send_scp.py`** (el
reimplemento Python) — el `send_scp.exe` del SBT da "Connection Failed" en las
mismas condiciones donde el .py conecta a la primera.

1. Flashear el firmware firmado por SWD (p.ej. `prebuilt/mfs_fix.sbin`).
2. Lanzar el listener (COM del Pico, p.ej. COM8):
   ```
   set MAXIM_SBT_DIR=C:\MaximSDK\Tools\SBT
   python tools/sscp/send_scp.py -c MAX32690 -s COM8 -i uart ^
     -x C:\MaximSDK\Tools\SBT\devices\MAX32690\scp_packets\writemaximcrk.zip -t 60 -v
   ```
3. **POR frío DURANTE la ventana de escucha** (el handshake del ROM ocurre justo
   al arrancar). Debe imprimir `Connected !` + el estado (`CRK : Not Exist`) y
   correr la sesión al 100%.
4. ⚠️ El final puede decir **"SCP session FAILED"** — ES COSMÉTICO si llegó al
   100%. **Verificar por el resultado, no por el exit code**: otro POR → la
   placa debe ARRANCAR el firmware (PC en `0x1000xxxx`) y `0x10801000` mostrar
   la CRK (`11d47194 …`). Verificado así en la primera placa virgen (USN
   `a488059220013c70020f06d70b`): CRK grabada y auto-arranque OK.

## Verificación de link SPE (por SWD, sin consola)

Globals de diagnóstico del firmware (direcciones del build 16-jul; revalidar con
el `.map`): `g_link[6]` (estado de link por puerto), `g_phyret[4]` (retorno de
lectura PHY; `0` = OK). Con el firmware corriendo, `g_link` = 1 en el puerto
conectado y `g_phyret` = 0 confirman el enlace. (`g_phyid1` puede leer `0xffff`:
es artefacto del diagnóstico que lee el ADIN1100 en modo C22 cuando es C45; no
implica PHY muerto si `g_phyret=0` y el link sube.)

## Artefacto listo para flashear

`prebuilt/mfs_fix.sbin` — imagen `mfs_pullup` con el binpatch de ERTCO, firmada y
**probada** (arranca y linkea). Flashear tal cual en placas con CRK presente.
