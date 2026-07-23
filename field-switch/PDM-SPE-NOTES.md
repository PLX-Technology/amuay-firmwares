# PDM / SPE — transmisión TPU→field switch: diagnóstico y reglas

> Bitácora 2026-07-22. Resultado final: **transmisión SPE funcionando** (TPU
> `spe0` → PDM → ADIN6310, con conmutación L2 verificada). La causa raíz de que
> "no pasaran datos" fue **strap de dirección MDIO del PDM ≠ dirección del slot**.

## Regla de oro: el strap del PDM debe coincidir con su slot

Cada slot físico del field switch lleva los nets de un puerto concreto del
ADIN6310, y el firmware espera el PHY de ese puerto en una dirección MDIO
concreta (`phyAddr = nº de macPort`). **El strap de dirección del ADIN1100 del
PDM debe coincidir con el slot donde se enchufa:**

| Slot físico (serigrafía) | Nets del conector | macPort firmware | **Strap addr requerido** |
|---|---|---|---|
| **Port 1** (J1) | `P2_TXD/RXD/TXC/RSTN/LINK…` | `macPort2` | **2** |
| **Port 2** (J3) | `P1_TXD/RXD/TXC/RSTN/LINK…` | `macPort1` | **1** |
| Port 3-6 | verificar nets `P<n>_*` en el esquemático | `macPort<n>` | `<n>` |

*(Verificar cada slot contra el esquemático MFS: el número del net `P<n>_*` del
conector es el que manda, no la serigrafía.)*

## Firma del síntoma cuando strap ≠ slot (para reconocerlo rápido)

- El enlace **entrena** igualmente (la TPU muestra `spe0` `LOWER_UP`, 10 Mbps):
  el ADIN1100 linkea con sus **defaults de hardware**, sin necesitar al SES. **El
  link-up NO prueba que los datos pasen.**
- El SES **no ve el PHY**: `SES_ReadPhyReg` devuelve `0xffff` (busca otra addr).
- **No cruzan tramas**: contadores RX del switch a 0; la TPU nunca recibe nada.
- Al alinear strap↔slot: el SES lee el PHY (`PHYID = 0x0283`) y el tráfico fluye.

## Descartado durante el diagnóstico (no volver a perseguirlo)

1. **Potencia PoDL**: el camino de DATOS del PDM va por transformador (T1) y el
   ADIN1100 del PDM se alimenta del carrier vía MicroMod (`VDD3P3_BRD` etc.),
   NO de la potencia PoDL extraída (`POWER_SPE` es solo la SALIDA del LTC9111
   para alimentar aguas abajo). **Los datos pasan sin PoDL.**
2. **Master/slave**: si el enlace entrena (autoneg completa), el master/slave
   quedó resuelto — un conflicto real de M/S impide el link-up. Referencia de
   registros del ADIN1100 (datasheet Rev. C) por si hiciera falta:
   - `AN_ADV_MST` = MMD **7**, reg **0x0203**, **bit 4** — preferencia advertida
     en autoneg (1=master, 0=slave). Es la vía con autoneg activo (el default
     del firmware mfs).
   - `CFG_MST` = MMD **1**, reg **0x0834**, **bit 14** — forzado, solo con
     autoneg deshabilitado.
   - El pin **`MS_SEL`** del ADIN1100 fija el default de ambos por hardware.

## Procedimiento de prueba de transmisión (reproducible)

**Lado TPU** (CM5, interfaz `spe0` = ADIN1110, `192.168.50.1/24`):
```bash
ip -br link show spe0          # debe estar UP, LOWER_UP
ping -I spe0 -c 40 -i 0.2 -b 192.168.50.255   # generar trafico broadcast
ip -s link show spe0           # TX debe incrementar
```

**Lado field switch** (por Pico SWD, firmware `mfs_fix` — direcciones del build
que corre; revalidar con el `.map` si se reflashea):
| Global | Dirección | Qué es |
|---|---|---|
| `g_phyid1[4]` | `0x20099430` | PHYID de macPort1-4 — **`0x0283` = SES ve el ADIN1100** |
| `g_rx[4]` | `0x20097b60` | rxByte de macPort1-4 — el puerto del PDM debe **incrementar** |
| `g_tx[4]` | `0x20097b50` | txByte de macPort1-4 — flooding a los demás puertos con link |
| `g_link[6]` | `0x20097bac` | estado de link por puerto |

**⚠️ En `mfs_fix` las stats son una FOTO tomada una sola vez en el arranque**
(no se refrescan en el bucle). Para capturar tráfico: dejar un ping continuo en
la TPU y hacer POR del field switch — la foto del arranque capturará el tráfico.

**Criterio de éxito** (verificado 2026-07-22): `g_phyid1` con `0x0283`,
`g_rx[puerto del PDM]` > 0, y `g_tx` de los otros puertos con link mostrando el
mismo byte count (= el switch conmutó/floodeó los broadcasts). ✓

## Picos de corriente en la fuente del field switch — EXPLICADOS (benigno)

> 2026-07-23. Síntoma: la fuente externa del FSW (24 V) oscila
> **27 mA ↔ 270 mA ↔ 27 mA** periódicamente. Diagnóstico cerrado por
> experimento A/B controlado.

**Causa: el PSE propio del field switch.** El FSW lleva su **propio LTC4296**
(sección "POWER SOURCE (PSE)" del esquemático MFS — el que alimentará los ATT
aguas abajo). El firmware `mfs` lo configura al arrancar (clase APL con
overrides del overlay, `port_prebias` + `port_en`), y con los puertos **vacíos**
el chip entra en su ciclo autónomo de PSE sin PD:

```
power-up (inrush ≈ cientos de mA) → nadie consume MFVS → tMFVDO expira
→ settle-sleep → restart → power-up otra vez…
```

Cada reintento = un pico. **Es benigno**: ese camino corre con todas las
protecciones del chip (soft-start, foldback, timers de inrush/MFVS) y el ciclo
**cesa por puerto en cuanto se conecta un PD real** que consuma.

**Experimento que lo probó (A/B):**

| Etapa | Estado | ¿Picos? |
|---|---|---|
| Firmware corriendo | LTC configurado por el firmware | Sí |
| MCU en **halt** (SWD) | LTC configurado, firmware congelado | **Sí** → máquina autónoma del LTC, no actividad del firmware |
| Chip retenido en **ROM** tras POR | LTC **jamás configurado** (AUTO=GND) | **No** → confirmado |

**Truco para "el MCU jamás arranca" sin línea de reset:** la línea srst del
Pico **no llega** al MAX32690 en J11 (probado: `adapter assert srst` no resetea).
Alternativa determinista y reversible: **borrar la página 0 de la flash**
(`flash erase_address 0x10000000 0x4000`) → el secure-boot rechaza la imagen →
el chip queda en ROM para siempre tras el POR → el firmware nunca corre.
Restaurar: re-flashear `prebuilt/mfs_fix.sbin` + POR. (Ojo: tras el POR de
restauración el ROM tarda ~1-2 s validando la firma — un PC leído en
`0x0000xxxx` justo tras el POR puede ser el ROM aún validando; releer.)

**Dato útil que dejó el experimento:** el consumo real de la **lógica** del FSW
es el baseline = **27 mA @ 24 V ≈ 0.65 W** (ADIN6310 + MAX32690 + PHYs, sin
entregar potencia PSE). Referencia para dimensionar la clase SPoE del enlace
MPS→FSW.

## Firmware Clase 11 del FSW (`prebuilt/mfs_class11.sbin`) — patch de DATOS

> 2026-07-23. El PSE propio del field switch pasó de `LTC4296_PSE` (APL con
> overrides — el modo que cicleaba/blinkeaba) a **`LTC4296_PSE_SCCP_CLASS_11`**
> en los 4 puertos, para operar a 24 V con entrega negociada. **Flasheado y
> arrancando** (probado).

Como las builds frescas del mfs no arrancan (bloqueo de secure-boot, ver abajo),
el cambio se hizo como **binpatch de datos** sobre `mfs_fix.sbin` — la clase de
cada puerto vive en la struct `ltc4296_config_0` (rodata del devicetree), así
que cambiarla **no toca ni una instrucción**:

- Entrada por puerto (24 bytes): 16 B de gpio_dt_spec SCCPO/SCCPI +
  `power_class` (u8 + 3 pad) + `hs_resistor` (u32). Patrón buscable:
  `01 00 00 00 fa 00 00 00` (clase 1=APL, 250 mΩ) × 4 con stride 24.
- Patch: `01 → 03` (= `LTC4296_PSE_SCCP_CLASS_11`) en las 4 entradas.
- Script: `prebuilt/patch-class11.py` (extrae imagen del sbin, verifica patrón
  y binpatch de ERTCO, escribe `.bin` patcheado). Re-firmar con la receta de
  siempre (`jump_address=1000662c`).
- Verificación del sbin final: diff vs `mfs_fix.sbin` = solo los 4 bytes de
  clase + los 64 B de firma. ✓

**Esta técnica sirve para cualquier cambio de CONFIG/datos del mfs** (clases,
hs-resistor, etc.) mientras dure el bloqueo de builds — solo código nuevo sigue
bloqueado.

**Comportamiento tras el cambio (esperado y verificado):** los puertos PSE
vacíos ya **no ciclean** (adiós picos 27↔270 mA y LEDs de PSM blinkeando — eso
era el power-up ciego del modo APL). En SCCP solo se entrega tras clasificar un
**PD real** (LTC9111 del PDM, re-strapeado a Clase 11: `CLASSV=GND,
CLASSC=FLOAT`). El LTC9111 clasifica **pasivamente** (sin MCU) → una cascada
FSW padre→PSM→cable→PDM→FSW hijo se enciende sola con el POR del padre si todo
está cableado antes (el mfs negocia UNA vez al arrancar, sin retry hot-plug).
Presupuesto Clase 11: 3.2 W por enlace (lógica FSW 0.65 W + 2-3 ATTs, sin
nietos FSW).

## Diagnóstico SCCP Clase 11 — el PSM tira la línea SCCP a bajo (2026-07-23)

> Primer intento de entrega SPoE negociada real (FSW padre Clase 11, PSM en
> slot Port 4, cable a PDM Clase 11). Síntoma: **silencio total** — sin LED,
> sin actividad, 0 V en el par. Diagnóstico completo por SWD **sin consola y
> sin recompilar**, manejando el SPI0 y el GPIO2 del MAX32690 a registro
> pelado desde openocd (scripts `ltc_dump.tcl`/`sccp_*.tcl` de la sesión).

**Traza del fallo (verificada registro a registro):**

1. `probe()` → Vin GADC = **23.7 V** ✓ (ventana Clase 11 20-30 V pasa)
2. Prebias escrito ✓ (`PxCFG1=0x0108`), clasificación habilitada ✓
   (`PxCFG0=0x2041`), puerto entra en **SEARCHING inmediatamente** ✓
   (la ventana de 4 ms del driver NO es el problema)
3. `sccp_reset_pulse()`: **la línea SCCP debe reposar ALTA** (protocolo tipo
   1-Wire) → **lee BAJA** → `ADI_LTC_SCCP_PD_LINE_NOT_HIGH` → `port_disable`
   silencioso. Fin.

**A/B concluyente (línea sccpi por puerto, en clasificación):**

| Slot | Línea SCCP |
|---|---|
| Vacío (puerto 3, y puerto 2 tras sacar el PSM) | **1 = alta** ✓ |
| Con PSM (solo, sin cable) | **0** ✗ |
| Con PSM + cable + PDM | **0** ✗ |

→ **El módulo PSM tira/carga la línea SCCP a bajo por sí solo.** Placa, slot,
cable y PDM exonerados. Como los PSM son los mismos del power switch, esto
explica retroactivamente el viejo "la cadena SCCP no completa" del MPS:
**ningún PSM ha dejado pasar una clasificación SCCP jamás.** El driver de ADI
se validó en su demo D2Z, donde el bias de la línea SCCP existe en hardware;
en el eco-sistema PSM ese reposo-alto no se da.

**Para hardware (Mayker):** nets del slot: `P<n>_SW` / `P<n>_SCCPI` /
`P<n>_SCCPO` (pines 19/21/23). Revisar en el PSM cómo se acopla la línea SCCP
al par y dónde está su bias/pull-up. Medible en DC en el pin SCCPI del slot
con/sin módulo. Nota: `DET_VLOW` en PxST con PSM insertado (la entrada del
módulo carga la salida del puerto).

### ROOT CAUSE FINAL (mismo día, medido con el GADC del propio LTC4296)

Midiendo la tensión de salida del puerto con el ADC interno del chip
(`GADCCFG=0x48` = Vout puerto 2, conversión (código−2048)×35 mV):

| Configuración | Vout durante probing de detección |
|---|---|
| Slot vacío (puerto 3, control) | **5145 mV** — probing sano, `DET_VHIGH`, sccpi=1 |
| PSM + cable + PDM | 35-70 mV |
| PSM sin cable | **70 mV** — igual |

→ **El módulo PSM presenta un CORTO DC a través de su par de potencia por sí
solo.** La corriente de sondeo (IVALID 1-2.5 mA) muere en el corto → nunca hay
firma de detección (siempre `DET_VLOW`) → nunca hay tensión de
clasificación → la línea SCCP nunca reposa alta → el handshake es imposible.
La cadena de sensado es correcta (el comparador del PSM reporta la verdad).

**Este único defecto explica TODO el histórico:** el "SCCP no completa" del
MPS, el OVERLOAD a ~1.16 A al forzar entrega (foldback contra el corto), los
síntomas de snubber/low-side, y el camino de corriente sostenida que quemó
R92 en la quema #2 (con Q17 en corto, el corto DC del módulo cerró el lazo).

**Localización para Mayker (óhmetro, módulo suelto):** medir
`PWR_P`↔`PWR_N` / entre los conductores del par: se esperan MΩ, habrá Ω.
Sospechosos: (1) **U3 (BSS123) + R16 4.99 Ω** — FET de escritura SCCP
atascado en ON (invertir SCCPO desde el MCU no cambió nada → posible net
roto carrier→módulo dejando el gate a su default); levantar R16 y re-medir.
(2) **Red de acople**: center-taps de T1 vía R14/R15=0 Ω + L1 — lazo DC por
el cobre del devanado si faltan bloqueos DC del lado de línea.
Verificación del fix: con el corto fuera, el puerto debe mostrar ~4-5 V de
probing (script `tools/ltc4296-swd/` lo mide en 2 min).

Descartado por experimento en el camino: polaridad de SCCPO (A/B sin
efecto), ventana de 4 ms del driver (SEARCHING entra inmediato), breaker de
lado bajo (retorno interno LSNS0→GND 17 Ω siempre activo; limpiar GFLTEV no
cambió nada), sig-override (no altera la línea).

**Hallazgos colaterales del mismo diagnóstico:**

- **Brownout por límite de fuente**: con el límite a 200-300 mA, el intento de
  entrega APL hacia el PDM desplomó el rail → **UVLO_DIGITAL → el LTC4296 se
  resetea y pierde toda la config** (GCMD vuelve a LOCK 0xA0, CFG0 a default
  0x0002) → un solo parpadeo y silencio. Para pruebas de entrega a 24 V:
  **límite ≥1 A** (el ACL del puerto limita ~250 mA con sense 0.24 Ω y el
  breaker de lado bajo corta a ~0.97 A; a 24 V no existe el mecanismo de las
  quemas del MPS, que fue transitorio de 50 V).
- **No insertar/sacar módulos en caliente**: resetea el FSW (glitch de rail).
- Un PDM suelto (sin carga en POWER_SPE) puede negociar (el LTC9111 clasifica
  alimentado de línea) pero **no retener** potencia (sin consumo MFVS →
  dropout cíclico). Para entrega sostenida, PDM en slot Port 1 de un FSW hijo.

## Garantía de no-entrega-ciega (verificado en banco 2026-07-23)

Pregunta de Mayker: *"¿el puerto entrega potencia directa o espera negociar?"*
**Respuesta verificada: JAMÁS entrega sin negociar.** Cadena de compuertas
(todas obligatorias, cualquier fallo → `port_disable`):

1. `AUTO` bajo (R77 100k pull-down; `GIOST.PAD_AUTO=0` leído en vivo) → modo
   gestionado: el chip no puede auto-energizar. Doble candado: el default de
   fábrica `PxCFG0=0x0002` (`HW_EN_MASK`) enmascara el enable por hardware.
2. Vin en ventana de clase (GADC) → 3. firma de PD válida (4.05-4.55 V bajo
   sondeo de 1-2.5 mA) → 4. handshake SCCP respondido → 5. clase compatible
   → solo entonces `set_port_pwr`.

Tras entregar, la supervisión **MFVS sigue armada** (`set_port_pwr` no
deshabilita timers): si el PD deja de consumir >2.5 mA, el chip retira la
potencia solo (tMFVDO) y cae a VSLEEP. Además el mfs negocia **solo al boot**
(sin retry hot-plug) — conectar después del POR no entrega nada.

Verificado tras POR limpio con PSM sano y nada conectado: los 4 `PxCFG0 =
0x0000` (el firmware deshabilitó todo al no hallar PD), sin sondeo, **LED del
PSM apagado**. (Un LED fijo con ~5 V en el par = puerto retenido en
clasificación por el debugger — sondeo de µA del estándar, no entrega.)

## 🏆 PRIMERA ENTREGA SPoE NEGOCIADA (2026-07-23) — Clase 11 funcionando

**Cadena:** FSW padre (`mfs_class11`, 24 V externo) → PSM sano en slot Port 4
→ cable SPE → PDM sano re-strapeado a **Clase 11** en slot Port 1 del FSW
hijo (hijo sin fuente externa). POR del padre → la negociación del boot
completó TODA la secuencia por primera vez:

| Registro | Valor | Significado |
|---|---|---|
| `P2EV` | 0x0200 | `VALID_SIGNATURE` — el LTC9111 del PDM presentó firma (4.05-4.55 V) |
| `P2ST` | 0x3e12 | estado **DELIVERING** + `PI_POWERED` + `POWER_STABLE` HI/LO |
| `P2CFG0` | 0x0021 | estado final correcto (`SW_EN\|POWER_AVAILABLE` tras `END_CLASSIFICATION`) |
| GADC Vout | **23 555 mV estables** | 24 V negociados en el cable |

**El corto no era solo el PSM: el PDM hijo TAMBIÉN estaba perforado.** Tras
reparar el PSM (C10), la línea volvió a caer con el PDM original conectado
(cable exonerado con extremo abierto: línea alta). Sustituido por un PDM
sano + re-strap Clase 11 → entrega. **⚠️ CRIBAR TODOS los módulos PSM/PDM
con óhmetro (par↔par, ambas polaridades): ~4 Ω = perforado; sanos = decenas
de kΩ o más.** Hipótesis de causa: transitorios de 50 V/hot-plug de la era
MPS perforaron el cap de línea (C10 en el PSM; equivalente en el PDM).

**⚠️ PENDIENTE ABIERTO — el FSW hijo no arranca con la potencia entregada:**
Iout ≈ 6 mA (~0.14 W) cuando la lógica del hijo necesita ~28 mA @ 24 V. Los
23.5 V llegan al PDM pero el hijo no enciende → revisar el camino
**`POWER_SPE` (salida del LTC9111) → `PSE_OUT` → rail del hijo** (J13/D10 y
el OR-ing con la entrada externa en el esquemático MFS). Nota: 6 mA cae en
la banda gris del MFVS (2.5-10 mA) — la entrega se sostiene de milagro; si
el chip la considera ausente, irá a settle-sleep (VSLEEP 3.4 V).

## Pendiente conocido (bloqueo para cambios de código en el mfs)

**Las builds frescas del firmware mfs no arrancan** (el secure-boot no las
valida; se quedan en ROM, PC `0x0000xxxx`), aunque estén bien construidas
(offset 0x100, `SRAM_VECTOR_TABLE=n`, header/vectores correctos) y re-firmadas
varias veces. Solo arranca el `mfs_pullup` pre-existente y su binpatch
(`prebuilt/mfs_fix.sbin`). Mientras no se resuelva, **no se pueden aplicar
cambios de código** al field switch (solo binpatches puntuales). Hay un bloque
de master/slave por firmware ya escrito en `main.c` (advertir slave en un
puerto, con guard anti-0xffff) que quedó sin probar por esto.
