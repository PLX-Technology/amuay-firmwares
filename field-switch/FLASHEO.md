# Field switch (MFS) — firmware en uso y cómo grabarlo

Rama `mfs`. Contiene **un solo firmware**, el que está corriendo en el banco,
y todo lo necesario para grabarlo en una placa nueva.

---

## 1. El firmware

```
prebuilt/mfs_clean_class13.sbin      615 792 bytes
```

| | |
|---|---|
| Switch | ADIN6310, 6 puertos SPE (sin RJ45) |
| PSE | LTC4296, **SPoE Clase 13** (50 V) en los slots Port 2 a 5 |
| MCU | MAX32690, imagen firmada para secure boot |

⚠️ **El Port 6 no entrega potencia.** Es el `port4` del LTC4296 y **no está
declarado** en el devicetree. Añadirlo se intentó y **la imagen dejó de
arrancar** — ver §6.

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

**COM del Pico, 9600 baudios, 8N1.** El conector `uC SWD` (J11) lleva
`LPUART_TX/RX` puenteadas por la CDC-UART del Pico.

> **No son 115200.** Creerlo lleva a leer «silencio» donde hay decenas de KB.

---

## 4. Aprovisionamiento de la CRK (placa virgen)

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
| **Declarar `port4`** (Port 6) en el dts + overlay, con `sccpi/sccpo` en **P2.23/P2.24** (nets `P4_SCCPI`/`P4_SCCPO`, confirmados en el esquemático MFS) | La imagen **deja de arrancar**: `PC` en ROM, sin PSE ni switch. Si se reintenta, **bisecar**: primero solo el board dts |
| **Vigilante de enlace**: reiniciar la autonegociación (`AN_T1_CTRL` 7.0200 bit 9) del puerto caído | Reintenta (contador subía) pero el puerto sigue sin enlazar |
| Leer registros del PHY con **`SES_ReadPhyReg`** | Devuelve **ceros en los 6 puertos, incluidos los que funcionan**. ⚠️ **Canal de diagnóstico NO fiable**: validarlo contra un puerto bueno (debe leer PHYID `0x0283`) antes de creer ninguna lectura |

---

## 7. Mapa de slots

| Slot | macPort (= strap MDIO del PSM) | Puerto LTC (potencia) |
|---|---|---|
| Port 1 | 2 | uplink (PDM) |
| Port 2 | 1 | 0 |
| Port 3 | 0 | 1 |
| Port 4 | 5 | 2 ⚠️ camino de **datos averiado** |
| Port 5 | 4 | 3 |
| Port 6 | 3 | 4 ⚠️ **sin declarar: no da potencia** |

**El strap MDIO del PSM debe coincidir con su slot.** Si no coincide, el enlace
entrena igual pero **no cruza ni una trama** — *link-up no prueba datos*.
