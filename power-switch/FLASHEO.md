# Power switch (MPS-04P) — firmware en uso y cómo grabarlo en una placa virgen

Rama `mps`. Contiene **un solo firmware** — el que se está usando — y el
código exacto con el que se compiló, para que artefacto y fuente no vuelvan a
separarse.

> **Por qué existe esta rama.** El `.sbin` estaba commiteado pero su fuente no:
> el `app.overlay` del repo seguía en `LTC4296_PSE` y el `main.c` no tenía la
> negociación. El firmware que corría **no se podía reconstruir desde el repo**.
> Aquí ya sí.

---

## 1. El firmware

```
prebuilt/pse_safe_class13.sbin     617 412 B   <- FIRMWARE DE PRODUCCION (jump 0x1000650c)
prebuilt/pse_safe_class13.bin      617 092 B      (sin firmar, trazabilidad)

prebuilt/adin6310_provision.sbin   615 712 B   <- solo para aprovisionar (ver 3.bis)
prebuilt/adin6310_provision.bin    615 392 B      ⚠️ ENERGIZA SIN NEGOCIAR
```

> **Regenerado el 2026-07-29** desde el fuente de esta rama, asi que artefacto
> y codigo coinciden. Incluye los dos arreglos del hilo lector del ADIN6310
> (ver el commit "el hilo lector ... ya no muere al primer error"), que el
> `.sbin` anterior no tenia.

---|---|
| Placa | MPS-04P |
| Switch | ADIN6310 — 4 puertos SPE (J3–J6), RJ45, uplink PCIe a la TPU |
| PSE | LTC4296, **SPoE Clase 13** (50–58 V, 231 mA) en los 4 puertos |
| MCU | MAX32690, imagen firmada para secure boot |
| Cabecera | `HISWEDGD` · rom_version `010203ff` · load `0x10000000` · jump `0x100064f8` |

### Nunca entrega potencia sin negociar

Es la propiedad de diseño de este firmware, y la razón de que reemplazara al
anterior. Verificado en el fuente de esta rama:

- **Cero** llamadas a `ltc4296_force_port_pwr`, **cero** `PREBIAS_OVERRIDE_GOOD`,
  **cero** escrituras crudas para energizar.
- El único camino a la potencia es `ltc4296_retry_spoe_sccp()`, que clasifica el
  PD por SCCP, lee la clase del devicetree y **valida Vin en rango antes de
  energizar**. Sin PD al otro lado, el puerto queda apagado.
- La única escritura directa al chip va en dirección contraria: limpia
  `TLIM_DISABLE` (bit 4) y `MASK_LOWFAULT` (bit 5) de `GCFG` en cada arranque,
  es decir **enciende** el límite térmico y hace visible el fallo de línea baja
  sea cual sea el estado previo del chip.
- Protecciones por defecto intactas: TLIM, foldback, soft-start,
  `TINRUSH = 56.2 ms` finito, timers MFVDO/TOFF.
- **El board dts deja los cuatro puertos en `LTC4296_PSE_DISABLED`** (desde
  2026-07-29). Antes ponia `LTC4296_PSE`, que en el `probe()` del driver hace
  `prebias APL + port_en` = **energiza sin negociar**; solo lo salvaba que el
  `app.overlay` los subiera a clase 13. Un build sin ese overlay habria
  energizado los cuatro slots. Ahora quien decide entregar es siempre la
  aplicacion. **Cambio byte-neutro**: el devicetree resuelto no varia.

**Reintento cada ~5 s** en el bucle principal, solo sobre los puertos que aún no
entregan (`PxST` bits 2:0 ≠ 2); los que ya entregan no se tocan. Por eso no hace
falta un segundo POR: se graba y arranca sin los 50 V, y al conectar la
alimentación y el PD negocia solo en menos de 5 segundos.

Detalle de diseño del LTC4296-1: el `LGATE` es **único y compartido** (un solo
FET de retorno, Q18), no por puerto, y **no hay bit de firmware para
encenderlo**. Engancha solo al entrar en power-up. Lo que hace el firmware tras
negociar es **re-armar el disyuntor de baja** (`clear_ckt_breaker`) y verificar
que no vuelve a disparar.

---

## 2. ⚠️ Protocolo de energización de los 50 V

**Regla de oro: la tensión se sube con la perilla, nunca con el conector.**

Esto no es celo excesivo — hay **dos LTC4296 quemados** detrás de esta regla
(ver `LTC4296-BURN-AUDIT.md`). El mecanismo: un escalón de fuente viva sobre la
inductancia del cable contra los cerámicos de entrada produce un ring LC que
puede acercarse a 2×Vin (~100 V con 50 V). Eso deja margen **cero** contra el
rating de 100 V de los PSMN075 y empuja el clamp del TVS por encima de los 80 V
de abs max del chip. Una rampa no tiene escalón, así que no hay ring.

1. Fuente en 0 V / salida OFF. **Conectar el cable ANTES de energizar.**
2. Límite de corriente **200–300 mA** para pruebas sin carga; subirlo solo con
   entrega PoDL real (Clase 13 ≈ 231 mA por puerto + margen).
3. **Rampear 0 → 50 V** en 1–2 s.
4. Verificar corriente en reposo. Si entra en límite → apagar: hay un corto.
5. Para apagar: **0 V / salida OFF primero**, luego desconectar. Nunca bajo carga.

---

## 3. Grabar el firmware

Por SWD con el **MAX32625PICO** y openocd. **Sin los 50 V conectados.**

```bash
openocd -s <scripts> -c "adapter driver cmsis-dap" -c "adapter speed 500" \
  -f target/max32690.cfg -c "reset_config none" \
  -c "init" -c "halt" \
  -c "flash write_image erase pse_safe_class13.sbin 0x10000000" \
  -c "verify_image pse_safe_class13.sbin 0x10000000" -c "shutdown"
```

En este equipo: `C:/MaximSDK/Tools/OpenOCD/openocd.exe`, scripts en
`C:/MaximSDK/Tools/OpenOCD/scripts`.

> `checksum mismatch - attempting binary compare` seguido de `verified N bytes`
> es **normal**: openocd pasa a comparación binaria.

---

## 3.bis ⚠️ Aprovisionar el ADIN6310 de una placa nueva (OBLIGATORIO)

**Una placa nueva trae el ADIN6310 EN BLANCO** y el firmware de produccion
**NO consigue cargarselo**: la transferencia muere en el 40 % con
`SMP_PROG_SERVICE_NAK` -> `error 246` (`SES_FWU_ABORTED`) ->
`Firmware update failed (-101)`, y `SES_AddDevice` nunca completa. Sintoma:
el MAX32690 arranca (PC en `0x1000xxxx`) pero **ningun puerto funciona** y
`g_link` se queda todo a cero (el bucle principal nunca corre).

**El firmware que SI lo carga** es uno de jul-2026, anterior a las
protecciones del LTC4296:

```
prebuilt/adin6310_provision.sbin   615 712 B   jump 0x10005f84
```

### Procedimiento (2 pasos, verificado 2026-07-29)

> ⚠️⚠️ **LOS 50 V DEBEN ESTAR DESCONECTADOS.** Este firmware es de la epoca
> de las quemas y **energiza los puertos SIN NEGOCIAR** -- es lo que
> destruyo dos LTC4296. Sin el riel de 50 V no puede entregar nada y el paso
> es seguro; con el conectado, NO. El aprovisionamiento del ADIN6310 va por
> SPI y no toca el LTC4296.

1. Grabar `adin6310_provision.sbin` y hacer **POR de ~10 s** (un corte breve
   no resetea el ADIN6310: nuestro firmware quito el pulso de reset porque
   P1.8 resetea la placa entera). Esperar ~30 s.
2. Grabar `pse_safe_class13.sbin` y **POR de ~10 s** otra vez.

**El firmware del ADIN6310 PERSISTE entre cortes de alimentacion.** Tras el
paso 1 el switch queda provisto para siempre; el paso 2 solo devuelve el
firmware seguro al MAX32690.

### Como saber que funciono

Leer **`g_link[6]`** por SWD (`0x20097980` en el build de `pse_safe_class13`):

| Lectura | Significado |
|---|---|
| todo `0` | El bucle principal NO corre -> `SES_AddDevice` fallo, sigue sin aprovisionar |
| valores mezclados (p.ej. `1,0,1,1,1,0`) | **Configuracion completa**: AddDevice + InitializePorts + VLANs OK |

Confirmacion adicional con el log en RAM (§9): si el arranque va de
`SES_Init` a `AddDevice` en ~15 ms **sin ninguna linea de "Firmware update in
progress"**, el switch ya tiene su firmware. Si aparecen esas lineas, esta
cargandolo.

⚠️ **`g_link` NO sirve para juzgar puertos individuales** (una direccion MDIO
vacia se lee `0xffff` = link fantasma). Aqui se usa solo como testigo de que
el bucle principal se ejecuta.

---

## 4. Aprovisionamiento de la CRK (placa virgen)

Sin la **CRK** en OTP el ROM no valida la imagen firmada y **no auto-arranca**,
por muy bien escrita que quede la flash.

### ⚠️ No dar por hecho que viene de fábrica

Durante mucho tiempo se creyó que "los power switch ya traen la CRK de fábrica".
**Es falso como regla general**: vale solo para las dos unidades que se midieron
en jul-2025. La MPS-04P aprovisionada el **2026-07-29** llegó completamente
virgen (USN `a4880592200138f0010f04a0c0`). **Comprobar siempre.**

### Diagnóstico

Por SWD, desbloqueando el FLC y leyendo el OTP:

```tcl
mww 0x40029040 0x3a7f5ca3
mww 0x40029040 0xa1e34f20
mww 0x40029040 0x9608b2c1
mdw 0x10801000 8
```

| Lectura en `0x10801000` | Significado |
|---|---|
| `11d47194 243cc2e4 b46e6a5a 799f1d47 …` | **CRK presente** — nada que hacer |
| `ffffffff …` | **Virgen** — hay que aprovisionar |

**Validar el desbloqueo antes de creerte el `ff`**: leer también `0x10800000`
(INFO0), que debe devolver los trims de fábrica (`c4520000 00904902 …`). Si eso
también sale `ff`, el desbloqueo no prendió y el diagnóstico no vale.

### Escribirla

Solo la graba el **ROM por su bootloader SCP sobre serie**. Por SWD **no hay
forma**: se probó a mano y con el driver de flash de openocd, y **reporta éxito
sin grabar nada** — la zona está protegida por hardware.

```bash
cd tools/sscp
MAXIM_SBT_DIR=C:/MaximSDK/Tools/SBT \
PYTHONPATH=C:/MaximSDK/Tools/SBT/src/send_scp/src \
python send_scp.py -c MAX32690 -s COM<pico> -i uart -t 300 -v \
  C:/MaximSDK/Tools/SBT/devices/MAX32690/scp_packets/writemaximcrk.zip
```

**Claves del procedimiento:**

- Usar **`send_scp.py`** (reimplemento Python en `tools/sscp/`). El **`.exe` del
  SBT da "Connection Failed"** donde el `.py` conecta.
- **POR en frío DURANTE la ventana de escucha.** La ventana solo se abre al
  encender. Con `-t 60` se queda corta si hay que ir hasta la placa: usar
  **`-t 300`**.
- **El POR corta la alimentación de la PLACA, no el USB del Pico.** Si se
  desenchufa el Pico se va el COM y `send_scp` pierde el canal justo cuando lo
  necesita. Aquí el Pico sí va conectado — su CDC-UART es el canal del
  bootloader (el conector `uC SWD` lleva `LPUART_TX/RX`).
- Dependencias: `pip install progressbar2 pyserial`.
- El `SCP session FAILED` final es **cosmético** si llegó al 100 %: verificar
  releyendo el OTP.

Escribe la `maximtestcrk`, que es justo la clave con la que firmamos. Deja el
JTAG desbloqueado. **Es OTP: se escribe una sola vez.**

---

## 5. Comprobación obligatoria tras grabar

Este proyecto tiene historial de imágenes que verificaban byte a byte y no
arrancaban. **Leer el PC**, no mirar síntomas indirectos.

```bash
openocd ... -c "init" -c "halt" -c "reg pc" -c "resume" -c "shutdown"
```

- `PC` en `0x1000xxxx` → la aplicación corre ✅
- `PC` en `0x0000xxxx` → sigue en el ROM ❌ (revisar CRK y firma)

**Muestrear el PC 2–3 veces**: si varía dentro del rango de aplicación, corre de
verdad; un valor fijo puede ser un bucle de fallo.

> El arranque autónomo solo queda probado con un **POR en frío y el Pico fuera
> del USB**. Saltar a la aplicación al cerrar la sesión SCP no lo demuestra.

Globals de verificación por SWD de este build (revalidar contra el `.map` si se
recompila):

| Global | Dirección | Significado |
|---|---|---|
| `g_gcfg` | `0x20099178` | GCFG tras la aserción de protecciones |
| `g_lg_any` | `0x20097914` | 1 = al menos un puerto negoció |
| `g_lg_gfltev` | `0x20099170` | GFLTEV tras re-arme (**bit0 = LOW_CKT_BRK_FAULT**) |
| `g_retry_rc[4]` | `0x20097900` | rc de `retry_spoe_sccp` por puerto |
| `g_lg_deliver[4]` | `0x20097918` | 1 = puerto entregando |

**Criterio de éxito con PD real:** `g_lg_any = 1`, `g_lg_gfltev` bit0 = **0** y
`g_lg_pxev[q]` bits 0/1 = **0** en el puerto activo.

---

## 6. Compilar y firmar

```bash
cd ~/zephyr && source .venv/bin/activate
export ZEPHYR_TOOLCHAIN_VARIANT=gnuarmemb GNUARMEMB_TOOLCHAIN_PATH=/usr
west build -p always -b mps04p/max32690/m4 \
  samples/application_development/adin6310 -d build_final -- \
  -DLIB_ADIN6310_PATH=/home/tpu01/ADIN6310SWDR-Rel5.1.0
```

Sin `-DLIB_ADIN6310_PATH` el CMake falla en `zephyr_library_sources`.

```bash
sign_app.exe -c MAX32690 ca=zephyr.bin sca=fw.sbin header=yes \
  algo=ecdsa key_file=<SBT>/devices/MAX32690/keys/maximtestcrk.key \
  rom_version=010203ff load_address=10000000 jump_address=<__start>
```

`<__start>` sale del `zephyr.map` de **ese** build, sin `0x`.

**Cuatro cosas que si faltan producen firmware que nunca arranca:**

| | |
|---|---|
| `algo=ecdsa` | Por defecto usa RSA y falla la firma |
| `key_file=…maximtestcrk.key` | Sin él ni siquiera firma |
| **`load_address=10000000`** | Sin él mete `0x01020304` de relleno |
| `CONFIG_FLASH_LOAD_OFFSET=0x100` | En el `prj.conf` |

**Verificar la cabecera antes de grabar** — bytes 0..24 del `.sbin`:

```
48 49 53 57 45 44 47 44 | 01 02 03 ff | 10 00 00 00 | <len> | <jump>
     "HISWEDGD"           rom_version   load_address
```

`sign_app` además **firma inválido de forma intermitente**: si un `.sbin` bien
hecho no arranca, **re-firmar y reintentar** antes de sospechar del binario.

### Procedencia de este árbol (verificado 2026-07-29)

El fuente se recuperó de la TPU y se comprobó contra `build_final`:

| Fichero | Cómo se verificó |
|---|---|
| `prebuilt/pse_safe_class13.bin` | SHA-256 `18090fcc…` **idéntico** a `build_final/zephyr/zephyr.bin` |
| `src/main.c` | mtime 2 s anterior al `zephyr.bin`; contiene `retry_spoe_sccp` y ningún camino forzado |
| `app.overlay` | devicetree resuelto de `build_final` da `power-class = 0x5` (Clase 13) en port0–3 |
| `boards/…/…m4.dts` | ⚠️ el DTS **actual** de la TPU tiene `port4` añadido (experimento del Port 6 que dejó de arrancar). El bueno es el de esta rama = `.bak-preport4`, y el devicetree resuelto confirma solo port0–3 |
| `zephyr/drivers/sensor/ltc4296/` | los 2 ficheros que hoy difieren se tocaron el 27-jul, **después** del build; snapshots fechados prueban que no cambiaron entre el commit del 21-jul y el build del 23-jul |

---

## 7. Topología de puertos

| Puerto ADIN6310 | Conector | Modo | PHY |
|---|---|---|---|
| Port 0 | — | RGMII 1G | LAN7431 → PCIe → TPU (uplink) |
| **Port 1–4** | **J3, J4, J5, J6** | **RMII 10M** | ADIN1100 (módulos SPE enchufables) |
| Port 5 | RJ45 | RMII 100M | ADIN1300 |

Mapeo conector↔puerto **1:1** (J3=Port1 … J6=Port4), ni invertido ni corrido.

### RJ45 (Port 5) — ✅ RESUELTO 2026-07-30: los pull-ups estaban al riel de 0.9 V

**Causa raiz:** los pull-ups de strap de **`MACIF_SEL0` (pin 34)** y
**`MACIF_SEL1` (pin 35)** del ADIN1300 estaban atados al riel de **0.9 V**
(`0.9VADIN`, el de `DVDD_0P9`) en vez del de **3.3 V** (`3.3VADIN`, el que
alimenta `VDDIO1` pin 31 y `VDDIO2` pin 40).

Esos pines pertenecen al dominio de E/S, cuyo umbral de nivel alto ronda los
**2.3 V** (0.7 x VDDIO). Con 0.9 V el PHY los leia **bajos** — y bajo/bajo es
la combinacion de **RGMII por defecto**. Las resistencias estaban puestas pero
electricamente era como si no existieran.

Consecuencia en cadena: en RGMII el ADIN1300 espera un reloj de **125 MHz**,
pero el ADIN6310 con `port5` en RMII entrega **50 MHz**. Sin el reloj de su
modo, el nucleo digital no opera -> la autonegociacion no completa -> no hay
enlace, aunque el MDIO responda (va en su propio dominio de reloj).

**Fix:** mover los dos pull-ups de 10 kOhm al riel **`3.3VADIN`**.

### ★ Criterio de verificacion (por registro, sin instrumentos)

Leer `GE_RMII_CFG` (**MMD 0x1E, reg 0xFF24**) **recien arrancado, sin que el
firmware escriba nada** — via `SES_ReadPhyReg(SES_macPort5, 0x1EFF24, &v)`:

| Lectura | Significado |
|---|---|
| `0x0116` (bit0 = 0) | strap MAL: el PHY arranca en **RGMII** |
| **`0x0117`** (bit0 = 1) | **strap OK: arranca en RMII** |

Confirmacion final: **`g_link[5]` pasa de `0` a `1`** (`0x20097980` en el build
de `pse_safe_class13`) y el **LED del conector** enciende. Medido asi el
2026-07-30 tras el fix.

### ⛔ Por que NO se puede arreglar por software (probado a fondo)

El registro **SI es escribible** — se verifico pasando de `0x0116` a `0x0117`
por la via clause-45. **La nota de jul-2026 que decia lo contrario era FALSA**:
aquellas pruebas se hicieron con un modulo SPE en **DIP=1 colisionando** con el
ADIN1300 en el bus MDIO (delator: PHYID leia `0xBC00` = AND de dos
dispositivos; limpio lee `0x0283`/`0xBC30`).

Pero hay un candado real:

| Accion | `RMII_EN` | Aplica el modo? |
|---|---|---|
| Escribir `0xFF24` bit0 | ✅ se pone y persiste | ❌ no surte efecto |
| Reset **BMCR** (bit15) | ✅ lo conserva | ❌ no reinicializa la interfaz/PLL |
| Reset de subsistema **`0xFF0C`** bit0 | ❌ **lo borra** (re-lee straps) | ✅ si |

**El unico reset que aplica el cambio de interfaz es el que vuelve a muestrear
los straps.** O se pone el bit o se aplica, nunca las dos cosas ⇒ **es strap de
hardware o nada.**

> Vias de acceso a los MMD del ADIN1300 (medido): **clause-45**
> `SES_ReadPhyReg(mac, 0x1EFF24, &v)` **funciona**; el indirecto clause-22 por
> los registros `0x0D`/`0x0E` **devuelve siempre `0x0000`** — no usarlo.
>
> **Control de validez obligatorio antes de interpretar nada:** leer `ANAR`
> (reg 4). En un PHY real el campo selector es **siempre >= 1**; un `0x0000`
> ahi significa que el acceso no llega y **ninguna otra lectura vale**. Con el
> canal sano se leyo `0x01e1`.
>
> Registros utiles (del driver de Linux `drivers/net/phy/adin.c`):
> `GE_RMII_CFG 0xff24` (RMII_EN = bit0), `GE_SOFT_RESET 0xff0c` (bit0),
> `GE_CLK_CFG 0xff1f`, `GE_RGMII_CFG 0xff23`.

### Nota: el "kick" de re-inicializaciones NO era la solucion

El firmware `adin_ethkick` de jul-2026 hacia 32 re-inicializaciones barriendo
direcciones MDIO y entonces el RJ45 linkeaba. Probado el 2026-07-29 con 8
re-inits de la config correcta: **no sirve** (`g_link[5]` sigue 0, las 8
devuelven 0). Aquello compensaba el strap por otra via; con el strap bien
puesto **no hace falta ningun kick**.

### Bus MDIO — reglas que hay que respetar

Un **solo** bus MDIO (D15/D14): el ADIN1300 del RJ45 y los cuatro módulos SPE
cuelgan del mismo par `MDC`/`MDIO`.

- **El ADIN1300 vive en la dirección MDIO 1** ⇒ **ningún módulo SPE puede tener
  DIP = 1**. Usar 2, 3, 5, 6, 7…
- Al escanear hay que leer clause-22 **y** clause-45. El ADIN1300 es clause-22 y
  **no aparece** en un escaneo solo-C45 — falso negativo que cuesta horas.
- Los módulos SPE son **RMII**, no RGMII (el eval oficial de ADI usa RGMII; esta
  placa no). El ADIN6310 entrega el reloj de referencia de 50 MHz.

### ⚠️ `SES_GetLinkState` / `g_link` NO ES EVIDENCIA DE NADA

Una dirección MDIO vacía se lee `0xffff` por el pull-up del bus, y SES lo
interpreta como **link = 1**: genera "links fantasma" en puertos donde no hay
absolutamente nada. Para saber si un puerto SPE enlaza de verdad, leer el PHY:

| Registro (clause-45) | Qué dice |
|---|---|
| `1.08F7` B10L_STAT | **bit0 = LINK real** |
| `7.0201` AN_T1_STAT | bit5 = autonegociación completa |
| `7.0200` AN_T1_CTRL | bit12 = AN habilitada |

**Firmas diagnósticas:**

| Lectura | Significado |
|---|---|
| `0xffff` | slot **vacío** (bus flotando) |
| `0x0000` en los MMD del núcleo pero el MMD `0x1E` responde | módulo **alimentado pero SIN RELOJ RMII** → fallo de placa en ese slot |
| `0xBC00` en la addr 1 | **colisión MDIO** (`0xBC30 & 0xBC81`: ADIN1300 con un módulo en DIP=1) |

---

## 7.bis Diagnosticar por qué un puerto no negocia (SPoE / SCCP)

Los registros que expone el firmware **no distinguen un PD conectado de un
slot vacío**: los dos acaban en `retry_rc = 1` y `PxST = 0x3000`. Antes de
sospechar del firmware o de la configuración, hay que separar los casos con
dos medidas por SWD (`tools/ltc4296-swd/`, con el firmware corriendo, 2 min).

### Por qué `retry_rc = 1` no dice nada

`ADI_LTC_DISCONTINUE_SCCP` vale **1** y es el cajón de sastre de
`retry_spoe_sccp`: recoge `PD_DETECTION_FAILED`, `PD_CRC_FAILED` **y**
`PD_LINE_NOT_HIGH`. Solo `PD_NOT_PRESENT` (**4**) sale distinto. Un slot
vacío da 1 y un puerto con la línea SCCP clavada también da 1.

### Medida 1 — línea SCCP (`mps_sccp_ctrl.tcl`, solo lectura)

`sccp_reset_pulse()` aborta en su **primera instrucción**:

```c
/* check if the line is high before reset pulse */
if(!READ_LINE(dev))
	return ADI_LTC_SCCP_PD_LINE_NOT_HIGH;
```

`sccpi` es ACTIVE_HIGH con pull-up, así que **en reposo debe leer 1** tanto
si el slot está vacío como si hay un PD conforme (que idlea en alta
impedancia). **`sccpi = 0` en reposo ⇒ ese puerto no negociará jamás.**

**Control obligatorio antes de culpar al módulo:** leer también `sccpo`
(`OUTEN` en `GPIO2+0x0C`, `OUT` en `GPIO2+0x18`). Si los cuatro puertos
tienen el mismo estado de `sccpo` y solo uno lee `sccpi = 0`, el pull-down
es **externo al MCU**. Estado normal medido: `OUT = 0x00000000`,
`OUTEN = 0x00528000` (bits 15/17/20/22 = los cuatro `sccpo`).

### Medida 2 — tensión de sondeo (`mps_vout_ab.tcl`)

Con el puerto en clasificación, `GADC` sobre `Vout`:

| Vout de sondeo | Significa |
|---|---|
| **~5145 mV** | par sano **o abierto** (es también lo que da un slot vacío) |
| **35-70 mV** | **corto DC en el par** — módulo con `C10` perforado |

Comparar siempre contra un slot vacío en la misma pasada: es el control.

### Un slot vacío da `retry_rc = 1` y eso es NORMAL

No hay que perseguirlo. El FET que escribe en la línea SCCP vive **dentro
del módulo PSM**, no en la placa. Con el slot vacío no hay FET, así que
accionar `sccpo` no baja `sccpi` (medido: `sccpo1` de 0→1 y `sccpi1` sigue
en 1). El driver lo ve como `PD_LINE_NOT_LOW`, lo remapea a
`PD_DETECTION_FAILED` y sale con `DISCONTINUE_SCCP = 1`. Script:
`mps_pd_fet.tcl`.

Corolario: **`sccpo` accionado sin efecto sobre `sccpi` con un módulo
insertado** significa que el FET del módulo no responde — o está atascado
conduciendo (línea clavada a 0) o su gate no llega.

### Descartado: el pin AUTO (pin 13) cableado distinto que en el MFS

En el **MPS** el pin 13 (`AUTO`) va **directo a GND**; en el **MFS** lleva
pull-down y además llega a `P0.16` del micro. **No afecta al SCCP.** Medido
en el MPS: `GIOST = 0x0001`, o sea **`PAD_AUTO` (bit3) = 0 = modo
GESTIONADO**, que es justo lo que el firmware da por supuesto (el chip no
energiza solo, espera órdenes por SPI). En el MFS el estado en reposo es el
mismo: el devicetree **no declara ningún `auto-gpios`**, el firmware nunca
toca `P0.16`, y el `spi3` que se lo llevaría como `SCK` está **deshabilitado**
(la entrada de `pinctrl` es vestigial). Además `AUTO` es una **entrada** del
LTC4296: no puede clavar a masa la línea SCCP, que es otra red.

⚠️ **La versión segura es la del MPS.** El montaje del MFS es un footgun
latente: si `P0.16` llegara a quedar alto o flotando, ese LTC4296 pasaría a
**modo autónomo y energizaría sin que el host negocie**. Hoy está a salvo
solo por la resistencia de pull-down externa. Si se toca ese pin en firmware,
dejarlo siempre bajo.

De la misma lectura: `GCAP = 0x0025` → `NUMPORTS` = **5** (valida que la
lectura SPI es buena) y **`SCCP_SUPPORT` (bit6) = 0**. Es correcto y
esperado: en este diseño el SCCP **no lo hace el LTC4296**, lo bit-bangea el
MAX32690 por `sccpi`/`sccpo` a través del módulo PSM (`ltc4296_sccp.c`). No
hay que buscar ahí ninguna avería.

### Cómo se combinan

| `sccpi` reposo | Vout sondeo | Diagnóstico |
|---|---|---|
| 1 | ~5145 mV | slot vacío, o PD que no responde al pulso de presencia |
| 1 | 35-70 mV | **par en corto** (criba con óhmetro `PWR_P`↔`PWR_N`) |
| **0** | ~5145 mV | **línea SCCP clavada a masa** con el par de potencia sano — cableado/PDM/módulo, no el firmware |
| 0 | 35-70 mV | módulo perforado arrastrando también la línea |

Para localizar el punto de la línea clavada, ir quitando eslabones **con la
fuente apagada** (nunca hot-plug): primero el cable al PDM, después el
módulo, después mover el módulo a otro slot. Si `sccpi` sube a 1 al retirar
un eslabón, el culpable es ese.

**Sospechar de una masa común** entre el PSE y el PD: el LTC4296 hace
sensado de corriente por el **lado bajo**, así que `PWR_N` **no** está a
masa. Cualquier retorno de masa externo entre las dos placas (sonda SWD,
USB, malla del cable, misma fuente de banco) cortocircuita ese lado bajo y
se lleva por delante la detección y el SCCP. Es la misma regla que prohíbe
unir la masa de la ATT con la del field switch.

---

## 8. Pendientes de hardware

| Problema | Accion |
|---|---|
| **Slot SPE 4 no linkea con ningun modulo** (la falla se queda en el slot al intercambiar modulos; el modulo esta alimentado y contesta MDIO, pero su nucleo no responde = sin reloj RMII) | Medir con osciloscopio `P4_TXC` = pin 47 de J6 y la bola **B9** del ADIN6310, contra `P2_TXC` (pin 47 de J4, bola T3) que si funciona. Deben ser **50 MHz**. Reloj en B9 y no en J6.47 -> pista/soldadura abierta; sin reloj en B9 -> soldadura fria del BGA |
| **TVS de entrada sin margen** (ver `LTC4296-BURN-AUDIT.md`) | El clamp del SMBJ58A llega a ~93 V, por encima de los **80 V abs max** del LTC4296. Considerar un SMCJ de mayor Ipp o menor clamp |

✅ **RJ45 — RESUELTO** el 2026-07-30 (pull-ups de `MACIF_SEL` al riel
equivocado, ver §7). Aplicar el mismo fix a cada placa nueva y verificar con el
criterio del registro.

### ⚠️ Antes de energizar una placa por primera vez

**Cribar los modulos PSM/PDM con ohmetro entre `PWR_P` y `PWR_N`**: un modulo
sano da MOhm; **4 Ohm = C10 perforado** (le paso a uno, probablemente por los
transitorios de hot-plug de 50 V). Un modulo perforado presenta un corto casi
directo al energizar.

**Primera energizacion con los slots VACIOS.** Sin PD el firmware no entrega
nada (negocia o no entrega), asi se comprueba que el riel y el LTC4296 arrancan
sanos — `g_gcfg` debe pasar de `0xffff` a un valor legible — antes de arriesgar
ningun modulo.
