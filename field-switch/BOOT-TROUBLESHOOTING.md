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

## Causa raíz #1 — cuelgue en ERTCO (32 kHz) en el HAL de ADI

El board devicetree (`boards/adi/adin6310t1l/..._m4.dts`) habilita el ERTCO:

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
> west build -p always -b adin6310t1l/max32690/m4 <sample> -d <dir> -- \
>   -DLIB_ADIN6310_PATH=/home/tpu01/ADIN6310SWDR-Rel5.1.0
> ```

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
3. Si CRK ausente (`0xff`): aprovisionar la CRK por UART del bootloader
   (`send_scp -i uart -x writemaximcrk.zip`) antes de flashear.

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
