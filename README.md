# pacific-firmware — rama `mps`

> Rama curada del **power switch (MPS-04P)**: el firmware que se está usando, el
> código exacto con el que se compiló, y el procedimiento completo de grabado y
> de aprovisionamiento de CRK en
> **[`power-switch/FLASHEO.md`](power-switch/FLASHEO.md)**.
> El firmware del field switch vive en la rama `mfs`.

Firmware de los dos equipos del sistema de monitoreo de tanques:

| Directorio | Placa | MCU + switch | Función |
|---|---|---|---|
| [`power-switch/`](power-switch/) | **MPS-04P** | MAX32690 + **ADIN6310** | Switch Ethernet TSN: 4 puertos SPE (10BASE-T1L), RJ45 y uplink PCIe a la TPU (CM5) |
| [`att/`](att/) | **ATT** | STM32WBA65 + **ADIN2111** | Va dentro de un sensor **Varec 2500**: lee el encoder del sensor y transmite la medida por SPE al power switch |
| [`tools/`](tools/) | — | — | Flasher del STM32 por UART y script de aprovisionamiento de equipos vírgenes |

Ambos son aplicaciones **Zephyr**. Este repo contiene **solo lo propio** (apps + definiciones de placa);
el árbol de Zephyr y los módulos se obtienen aparte con `west`.

---

## Credenciales

**Nunca se commitean.** El script de aprovisionamiento lee `tools/provision.env`,
que está en `.gitignore`. Para usarlo:

```bash
cp tools/provision.env.example tools/provision.env
# editar tools/provision.env con los valores reales
```

---

## power-switch — MAX32690 + ADIN6310

### Compilar y flashear

```bash
west build -b mps04p/max32690/m4 power-switch
```

El MAX32690 usa **secure boot**: hay que **firmar** el binario y flashear el `.sbin`.

```bash
sign_app -c MAX32690 ca=zephyr.bin sca=fw.sbin \
         key_file=<MaximSDK>/Tools/SBT/devices/MAX32690/keys/maximtestcrk.key \
         algo=ecdsa header=yes load_address=0x10000000 jump_address=<__start>
```

`jump_address` = el símbolo `__start` del `zephyr.map` **de ese build** (cambia en cada compilación).

```bash
openocd -f interface/cmsis-dap.cfg -f target/max32690.cfg \
        -c "init; reset halt; program fw.sbin 0x10000000 verify; shutdown"
```

> **El secure boot solo arranca con un POR REAL (corte de alimentación).** Un `reset` de OpenOCD
> no re-ejecuta el ROM → doble fault → lockup, y parece que el firmware está roto cuando no lo está.
> Tras el POR hay que **esperar ~30–40 s** antes de leer el PC por SWD: el ROM valida la firma y
> carga el blob del ADIN6310 por SPI. Si lees antes, ves el PC en el ROM y parece un fallo.
>
> Al flashear, `checksum mismatch - attempting binary compare` seguido de `Verified OK` es **normal**.

### Topología de puertos

| Puerto ADIN6310 | Conector | Modo | PHY |
|---|---|---|---|
| Port 0 | — | RGMII 1G | LAN7431 → PCIe → TPU (uplink) |
| **Port 1–4** | **J3, J4, J5, J6** | **RMII 10M** | ADIN1100 (módulos SPE enchufables) |
| Port 5 | RJ45 | RMII 100M | ADIN1300 |

El mapeo conector↔puerto es **1:1** (J3=Port1 … J6=Port4), **ni invertido ni corrido**.

### Bus MDIO — reglas que hay que respetar

El ADIN6310 tiene **un solo bus MDIO** (pines D15/D14). El **ADIN1300 del RJ45 y los cuatro
módulos SPE cuelgan del mismo par** `MDC`/`MDIO` (R20/R21 son de 0 Ω).

- **El ADIN1300 vive en la dirección MDIO 1.** ⇒ **Ningún módulo SPE puede tener DIP = 1.**
  Usar 2, 3, 5, 6, 7…
- Al escanear el bus hay que leer **clause-22 *y* clause-45**. El ADIN1300 es clause-22 y **no
  aparece** en un escaneo solo-C45 — es un falso negativo que cuesta horas.
- **Los módulos SPE son RMII**, no RGMII (el eval oficial de ADI usa RGMII; esta placa no).
  El ADIN6310 entrega el reloj de referencia de 50 MHz.

### Cómo leer el estado (y qué NO creer)

> **`SES_GetLinkState` / `g_link` NO ES EVIDENCIA DE NADA.** Una dirección MDIO vacía se lee
> `0xffff` por el pull-up del bus, y SES lo interpreta como **link = 1**. Genera "links fantasma"
> en puertos donde no hay absolutamente nada. Comprobado poniendo un puerto en una dirección vacía.

Para saber si un puerto SPE linkea **de verdad**, leer el PHY:

| Registro (clause-45) | Qué dice |
|---|---|
| `1.08F7` B10L_STAT | **bit0 = LINK real** |
| `7.0201` AN_T1_STAT | bit5 = autonegociación completa |
| `7.0200` AN_T1_CTRL | bit12 = AN habilitada |

**Firmas diagnósticas** (distinguirlas ahorra mucho tiempo):

| Lectura | Significado |
|---|---|
| `0xffff` | slot **vacío** (bus flotando) |
| `0x0000` en los MMD del núcleo, pero el MMD `0x1E` responde | módulo **alimentado pero SIN RELOJ RMII** → fallo de placa en ese slot |
| `0xBC00` en la addr 1 | **colisión MDIO**: es `0xBC30 & 0xBC81` (AND cableado del ADIN1300 con un módulo en DIP=1) |

---

## att — STM32WBA65 + ADIN2111

### Compilar y flashear

```bash
export ZEPHYR_TOOLCHAIN_VARIANT=gnuarmemb
export GNUARMEMB_TOOLCHAIN_PATH=/usr        # sin esto falla con "FindZephyr-sdk.cmake"
west build -b att_wba65 att
python tools/stm32flash.py build/zephyr/zephyr.bin
```

Sin secure boot: se graba el **binario raw**. Entrar en bootloader = mantener el botón
**uC Bootload** (BOOT0) + reset. Consola en **COM6 a 115200 8N1** (el bootloader es **8E1**;
la app **8N1**).

> El bootloader ROM es **intermitente**: falla ~1 de cada 2 con `write FALLO en offset 0x…`.
> **La placa sigue en bootloader tras el fallo → basta reintentar.** `tools/stm32flash.py` ya
> reintenta por bloque.

### ⚠️ Relé de bypass — sin esto NO hay link SPE jamás

La ATT lleva un relé **K1 (Omron G6K-2F-Y)** gobernado por **Q2 (BSS138)**, cuya compuerta es
**`UC_BYPASS_EN` = PD14**. `R44` la mantiene en bajo, así que **el relé arranca desenergizado =
EN BYPASS**: puentea PORT1 con PORT2 y deja el **ADIN2111 fuera de la línea SPE**
(los puentes de soldadura SJ1/SJ3 no están poblados).

```c
gpio_pin_configure(gpiod, 14, GPIO_OUTPUT_ACTIVE);   /* UC_BYPASS_EN */
k_msleep(50);                                        /* conmutación del relé */
```

**Síntoma si falta**: los dos PHY perfectamente sanos (fuera de power-down, AN encendida,
TX a 2.4 V) pero **ceguera mutua** — `7.0207` (AN_T1_LP_H) = `0x0000` en **los dos** extremos.
Si ves eso, la línea está abierta; no pierdas el tiempo con la negociación.

### Erratas del esquemático (nets con nombres que el chip no tiene en ese pin)

El esquemático nombra los nets por la función que el diseñador pretendía, pero **el
STM32WBA65 no soporta esa función en esos pines**. Verificado contra
`stm32wba65rivx-pinctrl.dtsi`:

| Net del esquemático | Pin | Realidad | Solución |
|---|---|---|---|
| `SPI3_SCLK` | PA9 | PA9 **no tiene SPI3** (SCK solo en PA0/PA7); PC4 es SPI3_MISO exclusivo → caen en periféricos distintos | **SPI por bit-bang** (`zephyr,spi-bitbang`) |
| `TIM2_CH1` (ENCODER AA) | PA0 | PA0 **no tiene TIM2_CH1** (solo PA5/PA11/PB12, todos ocupados). **Ningún timer** tiene par de cuadratura en PA0+PA1 | **Decodificación en cuadratura por software** (EXTI0/EXTI1) |
| `UART0_TX/RX` | PA12/PA11 | Son **USART2** (AF3). Nombre engañoso, pero **funciona** | usar `usart2` |
| `USART2_TX/RX` (USART EXPANSION) | PA2/PA3 | PA2 **no tiene** USART2_TX y PA3 **no tiene ningún RX de UART** | **header inutilizable** tal como está |

### Nivel del SPI — rework de hardware necesario

El SPI del ADIN2111 opera a **1.8 V**. El level-shifter **IC6 (SN74AXC4T245)** venía con
`1DIR = 2DIR = 3V3` (A→B), pero el STM32 es el maestro y SCLK/MOSI/NSS necesitan **B→A**.
Rework aplicado:

- **`1DIR` → GND** (canal 1 = SCLK + MOSI, B→A)
- **`2DIR` → 3V3** (canal 2 = MISO, reworkeado a los pines 7/10, A→B)
- **NSS fuera del shifter**: PA5 directo a `ADIN_NSS`, aprovechando el pull-up R73 a 1.8 V
  → en el DTS: `cs-gpios = <&gpioa 5 (GPIO_ACTIVE_LOW | GPIO_OPEN_DRAIN)>`
  (el STM32 nunca saca 3.3 V a un pin de 1.8 V; el pull-up da el nivel alto)

NSS y MISO **no pueden compartir el canal 2** (un solo `2DIR`, direcciones opuestas) — de ahí
que el NSS vaya directo.

El ADIN2111 está strapeado en **OA-SPI** (OPEN Alliance TC6) → `spi-oa` + `spi-oa-protection`.

### RS-485

**USART2** (PA12 = TX, PA11 = RX) → **ADM2587E aislado**. La dirección es **automática**: el
comparador **ADCMP600** vigila la línea TX y gobierna `RE`/`DE` → **no hace falta GPIO de DE**.
Como `RE` y `DE` van atados, **el receptor se apaga mientras transmite: no hay eco local** y
probarlo exige un segundo nodo.

---

## Pendientes de hardware (para el electrónico)

| Equipo | Problema | Acción |
|---|---|---|
| **power-switch** | **RJ45 no linkea**: el ADIN1300 arranca en RGMII por strap, pero `port5` debe ir en RMII (el reloj común que necesita el SPE) | Poblar **pull-ups de 10 kΩ a VDDIO en `MACIF_SEL0` (pin 34, `P5_RXC`) y `MACIF_SEL1` (pin 35, `P5_RXCTL`)** → arranca en RMII. **Ya validado en otra placa.** |
| **power-switch** | **Slot SPE 4 no linkea con ningún módulo** (probado por intercambio: la falla se queda en el slot, los módulos están sanos). El módulo está alimentado y contesta MDIO, pero su núcleo no responde = **sin reloj RMII** | Medir con osciloscopio **`P4_TXC` = pin 47 de J6** y la **bola B9** del ADIN6310, contra `P2_TXC` (pin 47 de J4, bola T3) que sí funciona. Deben ser **50 MHz**. Si B9 tiene reloj y J6.47 no → pista/soldadura abierta; si B9 no tiene → soldadura fría del BGA. |

Descartado en ambos casos: **no es de diseño** (esquemático verificado pin por pin: los 4 puertos
SPE están cableados idénticos), **no es firmware** (misma configuración en los 4) y **no son los
módulos**.
