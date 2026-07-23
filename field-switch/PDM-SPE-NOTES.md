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

## Pendiente conocido (bloqueo para cambios de código en el mfs)

**Las builds frescas del firmware mfs no arrancan** (el secure-boot no las
valida; se quedan en ROM, PC `0x0000xxxx`), aunque estén bien construidas
(offset 0x100, `SRAM_VECTOR_TABLE=n`, header/vectores correctos) y re-firmadas
varias veces. Solo arranca el `mfs_pullup` pre-existente y su binpatch
(`prebuilt/mfs_fix.sbin`). Mientras no se resuelva, **no se pueden aplicar
cambios de código** al field switch (solo binpatches puntuales). Hay un bloque
de master/slave por firmware ya escrito en `main.c` (advertir slave en un
puerto, con guard anti-0xffff) que quedó sin probar por esto.
