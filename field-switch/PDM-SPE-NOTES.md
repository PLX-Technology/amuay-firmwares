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

## Pendiente conocido (bloqueo para cambios de código en el mfs)

**Las builds frescas del firmware mfs no arrancan** (el secure-boot no las
valida; se quedan en ROM, PC `0x0000xxxx`), aunque estén bien construidas
(offset 0x100, `SRAM_VECTOR_TABLE=n`, header/vectores correctos) y re-firmadas
varias veces. Solo arranca el `mfs_pullup` pre-existente y su binpatch
(`prebuilt/mfs_fix.sbin`). Mientras no se resuelva, **no se pueden aplicar
cambios de código** al field switch (solo binpatches puntuales). Hay un bloque
de master/slave por firmware ya escrito en `main.c` (advertir slave en un
puerto, con guard anti-0xffff) que quedó sin probar por esto.
