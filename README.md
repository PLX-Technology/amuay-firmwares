# pacific-firmware — rama `mfs`

> Rama curada del **field switch (MFS)**: el firmware que se está usando y el
> procedimiento de grabado y de aprovisionamiento de CRK en
> **[`field-switch/FLASHEO.md`](field-switch/FLASHEO.md)**.
> El firmware del power switch (MPS-04P) vive en la rama `mps`.

Firmware de los equipos del sistema de monitoreo de tanques:

| Directorio | Placa | MCU + switch | Función |
|---|---|---|---|
| [`field-switch/`](field-switch/) | **MFS** | MAX32690 + **ADIN6310** | Switch Ethernet TSN: 6 puertos SPE (10BASE-T1L), sin RJ45; el uplink a la TPU va por un **PDM** en el slot Port 1 |
| [`att/`](att/) | **ATT** | STM32WBA65 + **ADIN2111** | Va dentro de un sensor **Varec 2500**: lee el encoder del sensor y transmite la medida por SPE al field switch |
| [`tools/`](tools/) | — | — | Flasher del STM32 por UART y aprovisionamiento de CRK de equipos vírgenes |

Son aplicaciones **Zephyr**. Este repo contiene **solo lo propio** (apps + definiciones de placa);
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

## field-switch — MAX32690 + ADIN6310

Procedimiento completo (grabado, CRK, consola, firma, mapa de slots) en
**[`field-switch/FLASHEO.md`](field-switch/FLASHEO.md)**. En resumen:

```bash
west build -b mfs06/max32690/m4 field-switch
```

El MAX32690 usa **secure boot**: hay que **firmar** el binario y flashear el `.sbin`.

> **La consola es de 9600 baudios, no 115200.** Creer lo contrario lleva a leer
> «silencio» donde hay decenas de KB de datos.

> **El Pico debe estar desenchufado en el POR.** Con el CMSIS-DAP conectado al SWD
> durante el encendido, el ROM no salta a la aplicación y se queda dentro para siempre.


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
| **field-switch** | **El Port 6 no entrega potencia.** Es el `port4` del LTC4296 y **no está declarado** en el devicetree; declararlo dejó la imagen **sin arrancar** (PC en ROM, ni PSE ni switch) | Reintentar **bisecando** (primero solo el board dts) y **leer el PC tras el POR** antes de dar nada por bueno. Líneas SCCP confirmadas en el esquemático: **P2.23 = `P4_SCCPI`, P2.24 = `P4_SCCPO`**. Ver `field-switch/FLASHEO.md` §6 |
| **field-switch** | **Slot Port 4: camino de datos averiado.** El enlace entrena pero no cruza tramas | Ver el mapa de slots en `field-switch/FLASHEO.md` §7 |

Los pendientes del **power switch** (RJ45 y slot SPE 4) están en la rama `mps`,
en `power-switch/FLASHEO.md` §8.
