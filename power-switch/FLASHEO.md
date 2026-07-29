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
prebuilt/pse_safe_class13.sbin        617 252 B   <- LO QUE ESTA GRABADO en la placa
prebuilt/pse_safe_class13.bin         616 932 B      (sin firmar, trazabilidad)

prebuilt/pse_safe_class13_5port.sbin  617 252 B   <- lo que produce el fuente HOY
prebuilt/pse_safe_class13_5port.bin   616 932 B      (jump 0x100064f8, payload 3368f2ab)
```

> **Por que hay dos.** El 2026-07-29 el driver `ltc4296` paso a soportar un
> quinto puerto PSE, que necesita el field switch (rama `mfs`). Es codigo
> compartido, asi que el MPS tambien se recompila distinto: los dos bucles de
> configuracion de GPIO pasaron de `i < 4` a `LTC4296_MAX_PORTS`. **Mismo
> tamano, contenido distinto** (`3368f2ab` vs `18090fcc`).
>
> **El comportamiento del MPS no cambia**: sin `port4` en su devicetree la
> entrada queda a ceros (`LTC4296_PSE_DISABLED`) y el guardia `.port` hace que
> los bucles la salten. Verificado: devicetree resuelto con 4 puertos y
> `power-class = 0x5` en los cuatro, igual que antes.
>
> La placa sigue con `pse_safe_class13.sbin`. Al grabar
> `pse_safe_class13_5port.sbin`, artefacto y fuente vuelven a coincidir y este
> bloque se puede colapsar a un solo firmware.

| | |
|---|---|
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

### RJ45 (Port 5) — configurado, sin verificar

El firmware **sí** lo declara (`SES_rmiiMode`, `SES_phyADIN1300`, addr MDIO 1,
100 Mb full dúplex) y además intenta resolver por software el strap de hardware:

```c
/* El ADIN1300 arranca en RGMII. Habilitarle RMII poniendo
 * GE_RMII_CFG (MMD 0x1E, reg 0xFF24) bit0 = RMII_EN */
SES_ReadPhyReg(SES_macPort5, 0x1EFF24, &v);
SES_WritePhyReg(SES_macPort5, 0x1EFF24, v | 0x0001);
```

**No hay ninguna medida que confirme que enlaza.** El pendiente de hardware
(poblar pull-ups de 10 kΩ a VDDIO en `MACIF_SEL0` pin 34 y `MACIF_SEL1` pin 35)
sigue en el README raíz. Y `g_link[5]` **no sirve de prueba** — ver abajo.

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

## 8. Pendientes de hardware

| Problema | Acción |
|---|---|
| **RJ45 no linkea** (histórico; el parche por software del §7 no está verificado) | Poblar pull-ups de 10 kΩ a VDDIO en `MACIF_SEL0` (pin 34, `P5_RXC`) y `MACIF_SEL1` (pin 35, `P5_RXCTL`) → arranca en RMII. **Ya validado en otra placa.** |
| **Slot SPE 4 no linkea con ningún módulo** (la falla se queda en el slot al intercambiar módulos; el módulo está alimentado y contesta MDIO, pero su núcleo no responde = sin reloj RMII) | Medir con osciloscopio `P4_TXC` = pin 47 de J6 y la bola **B9** del ADIN6310, contra `P2_TXC` (pin 47 de J4, bola T3) que sí funciona. Deben ser **50 MHz**. Reloj en B9 y no en J6.47 → pista/soldadura abierta; sin reloj en B9 → soldadura fría del BGA. |

Descartado en ambos casos: no es de diseño (esquemático verificado pin por pin,
los 4 puertos SPE están cableados idénticos), no es firmware (misma
configuración en los 4) y no son los módulos.
