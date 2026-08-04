# Field switch (MFS) — firmware en uso y cómo grabarlo

Rama `mfs`. Contiene **un solo firmware**, el que está corriendo en el banco,
y todo lo necesario para grabarlo en una placa nueva.

---

## 1. El firmware

```
prebuilt/mfs_class13_port6.sbin   615 956 B   <- FIRMWARE ACTUAL (jump 0x10005fec)
prebuilt/mfs_class13_port6.bin    615 636 B      (sin firmar, trazabilidad)
prebuilt/mfs_clean_class13.sbin   615 792 B   <- anterior, sin el Port 6
```

> **Regenerado el 2026-07-29** desde el fuente de esta rama: artefacto y codigo
> coinciden. Incluye el Port 6 habilitado **y** los dos arreglos del hilo lector
> del ADIN6310 (un error de lectura ya no mata la comunicacion para siempre).
> ⚠️ **La placa de banco tiene grabada la version ANTERIOR** — regrabar con esta
> cuando se pueda hacer un POR.

| | |
|---|---|
| Switch | ADIN6310, 6 puertos SPE (sin RJ45) |
| PSE | LTC4296, **SPoE Clase 13** (50 V) en los slots Port 2 a 5 |
| MCU | MAX32690, imagen firmada para secure boot |

✅ **El Port 6 está habilitado Y VERIFICADO EN HARDWARE** (2026-07-30). Es el
`port4` del LTC4296. No bastaba con declararlo en el devicetree: hubo que
**arreglar el driver**, que solo leía cuatro hijos (`.port_config[]` se
quedaba en el índice 3). Ver §6.

**Verificación en la placa** (`tools/ltc4296-swd/`, rama `mps`):

| Comprobación | Resultado |
|---|---|
| Puerto instanciado | `GPIO2 OUTEN = 0x01528000` — **cinco** `sccpo`, bit 24 (P2.24) incluido. Con cuatro sería `0x00528000` |
| Entra en clasificación | `P4ST = 0x2023`, estado 3 = **SEARCHING** |
| La línea SCCP responde | `sccpi` de **0** (módulo en reposo) a **1** en SEARCHING — igual que los puertos que negocian |

⚠️ **El `.sbin` anterior de este fichero NO ARRANCABA.** Falló tres veces
(una esperando 30 s), con el PC dando vueltas por el ROM sin saltar jamás a
`0x1000xxxx`, pese a que su **firma verificaba** contra la CRK y su cabecera
parecía correcta (magic, `rom_version`, `load`, `jump`, `imglen` coherente con
el payload). **Se ha sustituido** por uno compilado desde el fuente de esta
rama y firmado con la receta de §6. Causa del rechazo: **sin identificar**.

⚠️ **Y NO tiene ningún problema de polaridad.** Se llegó a sospechar que el
Port 6 sufría la inversión de par del MPS porque su `sccpi` leía 0 durante
clasificación. **Era un artefacto de medida**: con el puerto sin instanciar el
firmware tampoco configura el pull-up de ese `sccpi`, así que el pin flotaba.
La inversión de par es **del MPS**, que tiene 4 slots; el MFS tiene 6
(incluido el del PDM) y no la sufre.

---

## 2. Grabar el firmware (placa que ya arranca)

Se hace por **SWD con el MAX32625PICO** y openocd.

```bash
openocd -s <scripts> -c "adapter driver cmsis-dap" -c "adapter speed 500" \
  -f target/max32690.cfg -c "reset_config none" \
  -c "init" -c "halt" \
  -c "flash write_image erase mfs_clean_class13.sbin 0x10000000" \
  -c "verify_image mfs_clean_class13.sbin 0x10000000" -c "shutdown"
```

En este equipo: `C:/MaximSDK/Tools/OpenOCD/openocd.exe`, scripts en
`C:/MaximSDK/Tools/OpenOCD/scripts`.

> El mensaje `checksum mismatch - attempting binary compare` es normal:
> openocd pasa a comparación binaria. Lo que vale es el `verified N bytes`
> posterior.

### ⚠️⚠️ El Pico DEBE estar desenchufado en el POR

Con el CMSIS-DAP conectado al SWD durante el encendido, **el ROM del MAX32690
no salta a la aplicación**: se queda dentro para siempre.

Síntomas de estar en el ROM: `PC` en `0x0000xxxx`, `GCR_PCLKDIS0` bit 9 = 1
(reloj de SPI0 apagado ⇒ todos los registros del LTC4296 leen `0x0000`),
ningún puerto entrega potencia. **Resiste POR repetidos y `reset run` por SWD.**

**Secuencia correcta de arranque:**

1. Pico **fuera** del USB
2. 50 V presentes y **todo cableado** — el SCCP negocia **una sola vez al
   arrancar**, sin reintentos
3. Encender
4. Solo entonces, reconectar el Pico si hace falta depurar

### Comprobación obligatoria tras grabar

```bash
openocd ... -c "init" -c "halt" -c "reg pc" -c "resume" -c "shutdown"
```

- `PC` en `0x1000xxxx` → la aplicación corre ✅
- `PC` en `0x0000xxxx` → está en el ROM ❌

Este proyecto tiene historial de imágenes que no arrancan. **Leer el PC antes
de dar nada por bueno**, no ir a mirar síntomas indirectos.

---

## 3. Consola serie

**COM del Pico, 8N1.** El conector `uC SWD` (J11) lleva `LPUART_TX/RX`
puenteadas por la CDC-UART del Pico.

⚠️ **La velocidad la fija el `prj.conf` del build. Ante silencio, PROBAR LAS DOS.**

| Firmware | Baudios |
|---|---|
| **Actual** (rama `mfs`, nivel 3) | **115200** — verificado 2026-08-04 |
| Versiones antiguas | 9600 |

Medido el 2026-08-04 sobre una unidad en servicio ya actualizada: a 115200 salen
los mensajes limpios; a 9600, 28 bytes de basura de trama.

> Este apartado afirmaba «9600, no son 115200», y esa frase **costó tiempo real**
> el 2026-08-04: llevó a dudar de medidas que eran correctas y a dar por buena
> una pista falsa. El fallo de fondo es tratar como fija una velocidad que
> depende del build. **Barrer 115200/57600/9600 antes de concluir «está muda».**

---

## 3.bis ★ Grabar SIN SWD, por serie — ver `GRABADO-POR-SERIE.md`

Verificado 2026-08-03 con firmware real: **se puede grabar el MAX32690 entero
por el LPUART del J11**, sin tocar `SWDIO`/`SWCLK`. Es la via para actualizar o
recuperar un field switch en campo con solo un cable USB, y la unica que quedo
disponible el dia que el SWD del Pico dejo de responder.

Dos cosas que hay que saber, y estan explicadas en ese documento:

- ⚠️ **Hay que rellenar la imagen a pagina completa de 16 KB**
  (`tools/sscp/pad_a_pagina.py`). Sin eso falla SIEMPRE en el 99 %.
- ⚠️ **No tocar la placa durante la transferencia.** Cada reset o corte mata la
  sesion, y ademas enmascara el fallo real.

## 4. Aprovisionamiento de la CRK (placa virgen)

> ⚠️ **Corregido 2026-08-03:** lo de "POR frio DURANTE la ventana" de mas abajo
> vale para una placa **virgen o sin aplicacion valida**... y en ese caso ni
> siquiera hace falta: el ROM **espera indefinidamente**, asi que basta con
> enchufar y lanzar. El POR solo es necesario cuando la placa **si** tiene
> firmware que arranca. Ver `GRABADO-POR-SERIE.md` §4.

Sin la **CRK** (Customer Root Key) en OTP, el ROM no valida la imagen firmada y
**no auto-arranca**, aunque la flash esté perfectamente escrita.

### Diagnóstico

Por SWD, desbloqueando el FLC y leyendo el OTP:

```tcl
mww 0x40029040 0x3a7f5ca3
mww 0x40029040 0xa1e34f20
mww 0x40029040 0x9608b2c1
# leer 0x10801000
```

| Lectura en `0x10801000` | Significado |
|---|---|
| `11d47194 243cc2e4 b46e6a5a 799f1d47 …` | **CRK de fábrica presente** — nada que hacer |
| `ffffffff …` | **Virgen** — hay que aprovisionar |

La mayoría de MAX32690 de muestra **ya traen la `maximtestcrk` de fábrica**,
que es justo con la que firmamos. Comprobar antes de escribir: **la CRK es OTP,
de una sola escritura.**

### Escribirla

Solo la graba el **ROM por su bootloader SCP sobre serie**. No hay forma por
SWD: se probó escribir el OTP a mano y con el driver de flash de openocd, y
**reporta éxito pero no graba nada** — la zona está protegida por hardware.

```bash
cd tools/sscp
MAXIM_SBT_DIR=C:/MaximSDK/Tools/SBT \
PYTHONPATH=C:/MaximSDK/Tools/SBT/src/send_scp/src \
python send_scp.py -c MAX32690 -s COM<pico> -i uart -t 60 -v \
  C:/MaximSDK/Tools/SBT/devices/MAX32690/scp_packets/writemaximcrk.zip
```

**Claves del procedimiento:**

- Usar **`send_scp.py`** (reimplemento Python en `tools/sscp/`). El **`.exe` del
  SBT da "Connection Failed"** donde el `.py` conecta
- Hacer un **POR en frío DURANTE la ventana de escucha** — la ventana del
  bootloader es breve y solo se abre al encender
- Dependencias: `pip install progressbar2 pyserial`
- El `SCP session FAILED` final es **cosmético** si llegó al 100 %: verificar
  releyendo el OTP o comprobando que ya auto-arranca

---

## 5. Firmar una imagen nueva

```bash
sign_app.exe -c MAX32690 ca=zephyr.bin sca=fw.sbin header=yes \
  algo=ecdsa key_file=<SBT>/devices/MAX32690/keys/maximtestcrk.key \
  rom_version=010203ff load_address=10000000 jump_address=<__start>
```

`<__start>` sale del `zephyr.map` del build, **sin `0x`**.

**Cuatro cosas que si faltan producen firmware que nunca arranca:**

| | |
|---|---|
| `algo=ecdsa` | Por defecto usa RSA y falla la firma |
| `key_file=…maximtestcrk.key` | Sin él ni siquiera firma |
| **`load_address=10000000`** | Sin él mete `0x01020304` de relleno |
| `CONFIG_FLASH_LOAD_OFFSET=0x100` | En el `prj.conf` del build |

**Verificar la cabecera antes de grabar** — bytes 0..24 del `.sbin`:

```
48 49 53 57 45 44 47 44 | 01 02 03 ff | 10 00 00 00 | <len> | <jump>
     "HISWEDGD"           rom_version   load_address
```

`sign_app` además **firma inválido de forma intermitente**: si un `.sbin` bien
hecho no arranca, **re-firmar y reintentar** antes de sospechar del binario.

### Compilar

```bash
cd ~/zephyr && source .venv/bin/activate
export ZEPHYR_TOOLCHAIN_VARIANT=gnuarmemb GNUARMEMB_TOOLCHAIN_PATH=/usr
west build -p always -b mfs06/max32690/m4 \
  samples/application_development/adin6310_mfs -d <dir> -- \
  -DLIB_ADIN6310_PATH=/home/tpu01/ADIN6310SWDR-Rel5.1.0
```

Sin `-DLIB_ADIN6310_PATH` el CMake falla en `zephyr_library_sources`.

### Procedencia de este árbol (verificado 2026-07-29)

El fuente commiteado **no era** el que compiló el `.sbin`: el `app.overlay`
seguía en `LTC4296_PSE` cuando la imagen que corre es **Clase 13**, y el
`main.c` tampoco coincidía. Recuperado de la TPU y fijado así:

| Fichero | Cómo se verificó |
|---|---|
| build de referencia | `build_class13nr`, localizado por el `__start` de la cabecera del `.sbin` (`0x10005fd4`) y confirmado extrayendo el payload: **`.sbin` = 256 B de cabecera + payload + 64 B de firma** |
| `app.overlay` | `app.overlay.bak-preport4` — coincide con el devicetree resuelto de referencia (`power-class = 0x5`, port0–3, sin port4) |
| `src/main.c` | `main.c.bak-preretry`, **idéntico** a `main.c.bak-precomm` ⇒ sin cambios entre el 23-jul 21:32 y el 27-jul 19:26, intervalo que **contiene** el build (22:20) |
| `prj.conf` | idéntico entre el 22-jul y el 27-jul; el `CONFIG_LOG` de la TPU es posterior |
| `boards/adi/mfs06/` | el DTS de esta rama, confirmado por el devicetree de referencia |

**Comprobación final:** compilando este árbol con la placa `mfs06`, el
devicetree resuelto sale **idéntico** al de `build_class13nr`, y en el
`.config` las únicas diferencias son `CONFIG_BOARD`, `CONFIG_BOARD_TARGET` y
los dos símbolos `BOARD_*`.

> ⚠️ **El árbol de la TPU está por delante de lo que hay grabado.** Su
> `app.overlay` tiene un quinto puerto (`port4`) y su `prj.conf` tiene
> `CONFIG_LOG=y`: son los experimentos del Port 6 y de la consola serie,
> **posteriores** a la imagen que corre. No están en esta rama a propósito —
> aquí se documenta lo que está grabado, no lo que se estaba probando.

---

## 6. Cosas probadas que NO funcionaron

No repetirlas sin leer antes por qué fallaron.

| Intento | Resultado |
|---|---|
| ~~**Declarar `port4`** (Port 6) en el dts + overlay hace que la imagen **deje de arrancar**~~ | **FALSO — refutado el 2026-07-29.** Declarar `port4` solo en el devicetree **no cambia ni un byte del binario** (compilado y comparado: mismo SHA-256 con y sin él). Un cambio que no altera la imagen no puede romper el arranque. El "PC en ROM" de aquel día vino de otra cosa; los dos sospechosos documentados son la **firma intermitentemente inválida de `sign_app`** y el **Pico conectado durante el POR**, que deja el ROM sin saltar nunca. Ver §6.1 |
| **Vigilante de enlace**: reiniciar la autonegociación (`AN_T1_CTRL` 7.0200 bit 9) del puerto caído | Reintenta (contador subía) pero el puerto sigue sin enlazar |
| Leer registros del PHY con **`SES_ReadPhyReg`** | Devuelve **ceros en los 6 puertos, incluidos los que funcionan**. ⚠️ **Canal de diagnóstico NO fiable**: validarlo contra un puerto bueno (debe leer PHYID `0x0283`) antes de creer ninguna lectura |

### 6.1 Port 6 — la causa real y cómo se habilitó (2026-07-29)

El driver `zephyr/drivers/sensor/ltc4296` solo instanciaba **cuatro** puertos:

```c
.port_config[0..3] = LTC4296_PORT_INIT(inst, portN),   /* no habia [4] */
for (int i = 0; i < 4; i++)  /* configuracion de GPIOs, x2 */
```

`port_config[4]` quedaba a ceros y `power_class == 0` es `LTC4296_PSE_DISABLED`,
así que el probe lo deshabilitaba en silencio. El array ya tenía sitio
(`LTC4296_MAX_PORTS = 5`) y el `enum` ya definía `LTC_PORT4`.

Cambios (los tres hacen falta):

1. **Driver** — `.port_config[4] = LTC4296_PORT_INIT_OPT(inst, port4)` y los dos
   bucles a `LTC4296_MAX_PORTS`. `_OPT` está condicionado a `DT_NODE_EXISTS`,
   para que el mismo driver siga sirviendo al MPS-04P: sin `port4` la entrada
   queda a ceros = deshabilitada, **nunca energizada**.
2. **Board dts** — nodo `port4` con `sccpi/sccpo` en **P2.23/P2.24** y
   **`adi,power-class = <LTC4296_PSE_DISABLED>`**.
3. **`app.overlay`** — `port4` con `LTC4296_PSE_SCCP_CLASS_13`.
4. `MFS_PSE_PORTS` 4 → 5 en `src/main.c`.

> ⚠️ **Por qué la clase va deshabilitada en el dts y SCCP en el overlay.**
> El `probe()` del driver bifurca según la clase: `LTC4296_PSE` hace
> `prebias APL + port_en`, que **ENERGIZA SIN NEGOCIAR**; solo las clases
> `LTC4296_PSE_SCCP_CLASS_*` llaman a `do_spoe_sccp`, que negocia. El board dts
> pone `LTC4296_PSE` en los puertos 0–3 y es el overlay quien los sube a clase
> 13 ⇒ **un build sin el overlay energizaría esos cuatro slots directamente.**
> Footgun preexistente; `port4` se declaró deshabilitado para no ampliarlo.

**Resultado medido:** arranca (PC variando en `0x1000aexx` tras POR en frío con
el Pico fuera) y los **cinco** puertos se sondean —
`g_short_vout = 5178, 5214, 5178, 5178, 5178 mV`, `g_short_mask = 0`, o sea los
cinco slots vacíos y sin cortos. **Pendiente:** validar entrega real con 50 V,
un PSM en el slot Port 6 y un PD que negocie.


---

## 7. Mapa de slots

| Slot | macPort (= strap MDIO del PSM) | Puerto LTC (potencia) |
|---|---|---|
| Port 1 | 2 | uplink (PDM) |
| Port 2 | 1 | 0 |
| Port 3 | 0 | 1 |
| Port 4 | 5 | 2 ⚠️ camino de **datos averiado** |
| Port 5 | 4 | 3 |
| Port 6 | 3 | 4 ✅ habilitado 2026-07-29 (ver §6.1) |

**El strap MDIO del PSM debe coincidir con su slot.** Si no coincide, el enlace
entrena igual pero **no cruza ni una trama** — *link-up no prueba datos*.

---

## 8. ★ `phyPullupCtrl = 1` — el enlace de datos de los seis puertos (2026-08-03)

**Sintoma:** el slot Port 4 (`macPort5`) entregaba potencia pero **no enlazaba
datos**. Se arrastraba desde julio como "problema conocido, sin resolver".

**Causa:** el **segundo campo** de `phyConfig` estaba a `0` en los seis puertos.
Con `phyPullupCtrl = 0` el SES **no llega a identificar el PHY** (`PHYID1` lee
`0x0000`), por tanto no lo configura, y **la autonegociacion se queda apagada**.

```c
/* antes */ { true, 0, N, SES_phySpeed10, ... }
/* ahora */ { true, 1, N, SES_phySpeed10, ... }
```

El arreglo era conocido desde el 21 de julio (rama
`fix/power-switch-phypullupctrl`) y **nunca se habia aplicado a esta rama**.

**Verificado en la placa:** `PHYID1 = 0x0283`, `AN control` bit12 = 1,
portadora real en `B10L`. Con una ATT conectada: `carrier=1 up=1` en su lado.

### Barrido de PHY de los 6 puertos

Se anaden globales `g_ph_*[6]`, refrescadas cada segundo, legibles por SWD.

⚠️ **Existen porque `g_link` no sirve para diagnosticar.** No distingue "el SES
no ve el PHY" de "el PHY esta pero sin portadora", y **da links fantasma en los
slots vacios** (lee 1 sin nada conectado). Solo un 0 de `g_link` es fiable.
Estas globales si separan los dos casos:

| Registro | Que dice |
|---|---|
| `g_ph_id1` | `0x0283` = el SES ve el ADIN1100; `0xFFFF`/`0x0000` = no lo identifica |
| `g_ph_b10l` | bit0 = portadora real a nivel 10BASE-T1L |
| `g_ph_anst` | bit2 = link, bit5 = AN completada |
| `g_ph_anctl` | bit12 = autonegociacion encendida |

Las direcciones **dependen del build**: sacarlas del `.map`, nunca reutilizar
las de otro. Como identificar el build que corre de verdad: leer la cabecera
del `.sbin` en flash (`mdw 0x10000000 8`) y buscar el `jump_address` en los
`.map`. Los builds `build_pullup` y `build_phydiag` comparten `__start`
(`0x100070f4`) y tamano, y **aun asi tienen SHA distinto** — pero las
direcciones de los simbolos coinciden, asi que para leerlos da igual cual sea.

### ⚠️ Este build lleva `CONFIG_LOG=y`

`prj.conf` tiene el registro **activado** a nivel depuracion. Es lo que corre en
la placa hoy y por eso se sube asi (artefacto y codigo deben coincidir), pero
**para produccion hay que volver a `CONFIG_LOG=n`** y valorar quitar el barrido
de PHY: son 36 lecturas MDIO por segundo que en operacion normal no hacen falta.
