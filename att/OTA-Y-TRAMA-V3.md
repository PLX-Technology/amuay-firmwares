# ATT — OTA por SPE (MCUboot) y trama v3 con temperatura

Estado a 2026-07-28. Cubre el arranque con MCUboot, la actualización remota
desde la TPU por el propio enlace SPE, la trama v3 (temperatura + humedad
reservada), los LEDs de actividad y los cambios que esto obliga en la
pasarela `varec-gateway`.

---

## 1. Qué hace ahora la ATT

| | |
|---|---|
| Arranque | **MCUboot** en `0x08000000`, app en `0x08010000` |
| Datos | trama L2 `0x88B5` **v3** cada 500 ms |
| Actualización | **mcumgr/SMP sobre UDP** por el enlace SPE, desde la TPU |
| Identidad | `tank_id` en EEPROM I²C externa (`AT24C64D`, 0x50) |
| Ambiente | temperatura del **ADT75** (I²C1, 0x48) |
| Calibración | **NO va aquí**: vive en la TPU (`scale`/`offset` por tanque) |

La ATT envía la **cuenta cruda del encoder**. Sin calibración configurada
(`dc==0`) su `level_mm` es igual a `count`, que es justo lo que queremos: la
conversión a unidades de ingeniería la hace la pasarela.

---

## 2. Particiones de flash

```
boot_partition   64 KB  @ 0x00000   MCUboot        (usa ~47 KB, 73 %)
slot0_partition 896 KB  @ 0x10000   imagen activa  (usa ~142 KB, 15.8 %)
slot1_partition 896 KB  @ 0xf0000   descarga OTA
libre           192 KB
```

### ⚠️ La trampa del `ranges;` — 4 ciclos de flasheo perdidos

Un nodo `fixed-partitions` de Zephyr **NUNCA lleva `ranges;`**. Con él, los
`reg` de las particiones se traducen al espacio de direcciones del padre y el
offset se suma dos veces:

```
CONFIG_FLASH_LOAD_OFFSET = 0x8000000   (debía ser 0x0)
→ firmware enlazado en 0x10000000, dirección que NO EXISTE en el STM32WBA65
```

**Síntoma: silencio absoluto en consola**, ni una línea de MCUboot, aunque el
flasheo verifique byte a byte — el contenido escrito era correcto, pero
compilado para otra dirección.

**Comprobación obligatoria antes de flashear:**

```bash
arm-none-eabi-readelf -h build_ota/mcuboot/zephyr/zephyr.elf | grep 'Entry point'
# debe dar 0x080xxxxx  (NO 0x100xxxxx)
```

Ante un "no arranca y no dice nada", mirar **la dirección de enlace antes que
el logging**. Activar `LOG_MODE_IMMEDIATE` no revela nada si no se ejecuta ni
una instrucción.

---

## 3. Compilación

`CONFIG_BOOTLOADER_MCUBOOT` es Kconfig de **sysbuild**, no de la aplicación:
va en `sysbuild.conf` con prefijo `SB_CONFIG_`, no en `prj.conf`.

```bash
cd ~/wba && source .venv/bin/activate
export ZEPHYR_TOOLCHAIN_VARIANT=gnuarmemb GNUARMEMB_TOOLCHAIN_PATH=/usr
west build --sysbuild -p always -b att_wba65 att-app -d build_ota
```

**Módulos que hay que traer** (no vienen en el workspace mínimo):

```bash
west config manifest.group-filter -- +bootloader
west update mcuboot mbedtls tf-psa-crypto zcbor
```

- `mbedtls` no compila sin `tf-psa-crypto`.
- **`zcbor` es obligatorio**: `MCUMGR` depende de él y sin él queda en `n`
  dejando solo un *warning* — la compilación pasa pero **no hay OTA**.

`sysbuild/mcuboot.conf` pone a MCUboot en `LOG_MODE_IMMEDIATE` + nivel `DBG`:
por defecto usa logging diferido y, si rechaza una imagen y se detiene, el
mensaje se queda en el buffer y **la placa parece muerta**.

---

## 4. Flasheo inicial por cable (una sola vez por placa)

MCUboot y la app firmada se combinan en una imagen para el bootloader ROM:

```python
boot = open('mcuboot/zephyr/zephyr.bin','rb').read()
app  = open('att-app/zephyr/zephyr.signed.bin','rb').read()
open('att_ota_full.bin','wb').write(boot + b'\xff'*(0x10000-len(boot)) + app)
```

```bash
python tools/stm32flash.py att_ota_full.bin COM6
```

Entrar en bootloader: mantener `uC Bootload`, pulsar y soltar `Board RESET`,
soltar `uC Bootload`.

**A partir de aquí la placa ya no necesita cable.** Conviene hacerlo **antes**
de instalar los sensores en los tanques.

### El flasheo por cable es eléctricamente justo

El bootloader ROM falla con frecuencia (`write FALLO en offset 0x…`, a veces
también el `mass erase`) — **reintentar, la placa sigue en bootloader**. Causa
de fondo: con el USB conectado, la placa **entera** se alimenta del USB (ver
§9) y el `VCCIO` del FT230X cuelga del raíl de 3.3 V a través de **R84**. Los
picos de corriente de la escritura de flash hunden ese raíl y corrompen la
línea serie. **Un cable USB corto y de buena sección cambia radicalmente la
tasa de fallo.**

---

## 5. Actualización OTA desde la TPU

Cliente: `~/smpvenv/bin/smpmgr` (`pip install smpmgr` en un venv aparte).

```bash
SMP="$HOME/smpvenv/bin/smpmgr --ip 192.168.50.186 --timeout 5"

$SMP image state-read                    # ver ranuras
$SMP image upload zephyr.signed.bin      # sube a slot1 (~5 kB/s)
$SMP image state-write <HASH_SLOT1>      # marca pending
$SMP os reset                            # reinicia -> MCUboot intercambia
```

⚠️ **`smpmgr upgrade` NO deja la imagen `pending`**: sube y resetea, pero no
la marca, así que MCUboot arranca la de siempre. Hacer los tres pasos por
separado.

El *warning* `Error reading MCUMgr parameters … ENOTSUP` es inocuo (el grupo
`os` no implementa esa consulta opcional).

### ⚠️ ABIERTO: el intercambio de ranuras

Con el modo por defecto **`swap-using-offset`** MCUboot rechaza la imagen y
**borra slot1**:

```
<inf> mcuboot: Image index: 0, Swap type: test
<dbg> boot_slots_compatible: slot0 has 112 usable sectors but slot1 has 111
<dbg> boot_validate_slot: slot 1 ... TLV off 150972, end 151304
<err> mcuboot: Image in the secondary slot is not valid!
```

`151304 − 143112 = 8192` = **exactamente un sector**: ese modo espera la
imagen secundaria desplazada un sector y la subida SMP la escribe al
principio.

Se cambió a `SB_CONFIG_MCUBOOT_MODE_SWAP_USING_MOVE=y` y **sigue fallando**,
pero **aún no se ha capturado el log de MCUboot con el modo nuevo**. Ese es el
siguiente paso — no volver a especular. Alternativas si no basta: dimensionar
slot1 un sector mayor que slot0, o `overwrite-only` (pierde la reversión).

**Todo lo demás del OTA está verificado**: subida, marcado, reinicio remoto y
autoconfirmación.

---

## 6. La red de seguridad: autoconfirmación condicionada al enlace

MCUboot arranca una imagen recién instalada **a prueba**. Si no se confirma a
sí misma, el siguiente arranque revierte a la anterior.

Aquí la confirmación **no es automática**: exige que el enlace SPE haya
transmitido de verdad.

```c
#define OTA_CONFIRM_TX_STREAK 4          /* ~2 s de enlace demostrado */

static void ota_confirm_check(bool tx_ok)
{
    if (!tx_ok) { streak = 0; return; }  /* un fallo rompe la racha */
    if (++streak < OTA_CONFIRM_TX_STREAK) { return; }
    boot_write_img_confirmed();
}
```

**Por qué importa**: existió un build con `BENCH_NO_SPE` que arrancaba
perfecto, consumía y entrenaba el PHY, pero **nunca transmitía**. Si una
imagen así se autoconfirmara dentro de un tanque, el sensor quedaría
inalcanzable y solo se recuperaría abriendo el tanque.

En consola, al arrancar: `OTA: imagen confirmada` o `A PRUEBA (revertirá si el
SPE no transmite)`.

---

## 7. Trama v3

La trama lleva campo `version` desde el principio; cada versión **añade
campos al final**, nunca reordena.

```c
struct att_frame {
    struct net_eth_hdr eth;   /* 14 B */
    uint32_t magic;           /* "VARE" 0x56415245 */
    uint16_t version;         /* 3 */
    uint16_t tank_id;
    uint32_t seq;
    int32_t  count;           /* encoder x4, CRUDO */
    int32_t  level_mm;        /* v2: nivel que calcula la ATT (referencia) */
    uint32_t edges;
    uint32_t errors;
    uint32_t uptime_ms;
    int16_t  temp_c10;        /* v3: 0.1 °C   */
    int16_t  humi_rh10;       /* v3: 0.1 % HR */
} __packed;
```

**`0x8000` (INT16_MIN) = dato no disponible.** Que el consumidor distinga
"0 grados" de "no hay sensor" no es un lujo: evita inventar lecturas.

| Versión | Payload | Añade |
|---|---|---|
| v1 | 28 B | — |
| v2 | 32 B | `level_mm` |
| v3 | 36 B | `temp_c10`, `humi_rh10` |

Registros de entrada Modbus (FC04) nuevos: **10 = temperatura**, **11 =
humedad** (0-9 ya estaban ocupados).

---

## 8. Temperatura y humedad

**Temperatura: IC13 = ADT75** en I²C1, A0/A1/A2 a masa ⇒ **0x48**. No tiene
driver propio en Zephyr, pero es **compatible a nivel de registros con el
LM75**, así que vale el genérico:

```dts
adt75: lm75@48 { compatible = "lm75"; reg = <0x48>; };
```

⚠️ **Mide la temperatura DENTRO de la carcasa del Varec**, la del entorno de
la electrónica — **no la del producto en el tanque**. Si hace falta
temperatura de proceso es otro sensor (sonda), por SV10 o por `ADC4_IN` de
SV6. Conviene aclararlo antes de que alguien tome esa cifra por la del tanque.

**Humedad: la ATT NO lleva sensor.** El campo queda **reservado** en la trama
y en el mapa Modbus, enviando el centinela. El conector de expansión **SV10 ya
saca `I2C1_SDA`/`I2C1_SCL`** más los raíles, que es donde iría un SHT4x o
HDC302x. Cuando exista, solo hay que rellenar `att_humi_rh10()`: **la trama y
el mapa Modbus no cambian.**

---

## 9. LEDs de actividad SPE (LED6 / LED7)

Cuelgan de las salidas `LED_0` de cada PHY del ADIN2111, **activas por nivel
bajo**:

```
3V3 ──▶|LED7──[R80]── pin 47 = P2_LED_0 / P2_SWPD_EN
GND ──────────[R66]── pin 48 = P2_LED_1 / P2_TX2P4_EN  (strap, NO hay LED)
```

Estaban apagados porque **el driver los desactiva salvo que el devicetree lo
pida**:

```c
.led0_en = DT_INST_PROP(n, led0_en),
if (!cfg->led0_en) { val &= ~LED0_EN; }   /* registro LED_CNTRL 0x8C82 */
```

**Fix: `led0-en;` en los dos nodos PHY.** No se pone `led1-en`: en `P2_LED_1`
solo hay R66 a masa como strap, activarlo sería poner una salida a pelear
contra un pull-down.

---

## 10. Cambios obligados en la pasarela (`tpu/varec-gateway`)

**El parser estaba en `FRAME_VERSION = 1` y la ATT ya emitía v2** ⇒ descartaba
todas las tramas en silencio. Por eso `ingest.enabled` estaba a `false` y se
había montado un `modbus_ingest` por RS-485 como parche.

Ahora `parse_att_frame()` acepta **v1, v2 y v3**:

```python
FRAME_FMT_BY_VER = {1: "!IHHIiIII", 2: "!IHHIiiIII", 3: "!IHHIiiIIIhh"}
```

Multiversión **a propósito**: con OTA la flota estará a medio actualizar, y
rechazar por versión sería tirar datos de sensores que funcionan.

Otros cambios: columnas `temp_c`/`humi_rh` en `samples_raw` (+ `ALTER TABLE`
en el arranque para bases ya desplegadas), `ingest.enabled=true`,
`modbus_ingest.enabled=false`.

La calibración ya estaba donde debe: `value = count * scale + offset` con
`scale`/`offset`/`unit` por tanque en la tabla `tanks`.

---

## 11. Riesgos abiertos antes de llevar esto a campo

### 🔴 La configuración se resetea a fábrica si cambia `struct att_cfg`

Se valida con magic + CRC. Si no cuadra:

```c
LOG_INF("EEPROM sin configuracion valida: escribo la de fabrica");
eeprom_write(eep, CFG_ADDR, &cfg, sizeof(cfg));   /* tank_id = 0 */
```

**Toda la flota perdería su `tank_id` de golpe y en silencio**, y la pasarela
descartaría sus datos (rechaza `tank_id=0` a propósito). Con 50+ sensores
dentro de tanques eso es una reasignación manual imposible.

**Requisito: versionar la config y migrarla campo a campo conservando
`tank_id`**, en vez de caer a fábrica.

Asignar id: `att_modbus_tcp.py wr 5 <id>` + `wr 9 165` (0xA5 = guardar).
Verificado que sobrevive al reinicio.

### 🔴 Todas las ATT comparten la misma MAC

`02:00:00:ad:21:11` (port1) y `…:12` (port2) están **fijas en el devicetree**.
Con varias en la misma red L2 habrá colisión, y la detección de `tank_id`
duplicado de la pasarela —que compara por MAC— no podrá distinguirlas.
Pendiente: derivarla del UID del micro.

### 🟡 La firma es la clave de DEMOSTRACIÓN de MCUboot

`bootloader/mcuboot/root-rsa-2048.pem` es **pública**. Generar una propia y
custodiarla antes de producción, o cualquiera podría firmar firmware para
estos sensores.

### 🟡 `VCCIO` del FT230X desde el raíl equivocado

Va al 3.3 V de la placa por **R84**, en vez de a `3V3OUT` (pin 8) como pide la
hoja de datos para diseños alimentados por USB. Con la placa alimentada por
SPE y el USB desconectado, la corriente se cuela de `VCCIO` a `VCC` por los
diodos internos y **el chip queda en un estado indefinido: al conectar el USB
no enumera**. Además degrada el flasheo (§4).

**Fix: mover el origen de R84 de `3V3` a `USB_3V3`.** Un solo resistor.

### Nota: SPE y USB no conviven de forma predecible

`SELECT 5V` son dos **diodos ideales LM66100 en OR** (U4 = SPE/EXT, U5 = USB)
sobre el mismo nodo `5VC`, ambos siempre habilitados: **gana la entrada que
esté un pelo más alta**, no hay prioridad. Si gana el USB, el consumo por SPE
se desploma y el LTC4296 retira la tensión por `MFVS_TIMEOUT` — que es la
protección funcionando, no un fallo. Varía de placa a placa por decenas de
milivoltios.

---

## 12. Verificado de punta a punta (2026-07-28)

```
hora       tank  count   value  edges  temp_C   humedad
16:05:02     24      0     0.0      0    30.0   sin sensor
```

encoder → ATT bajo MCUboot → trama v3 → SPE alimentado por SPoE → field
switch → TPU → pasarela → SQLite → MQTT/web.

| | |
|---|---|
| MCUboot arranca, valida RSA y encadena a slot 0 | ✅ |
| ADIN2111 (`PHY ID 283BCA1`) y DHCP | ✅ |
| SMP sobre UDP por SPE | ✅ |
| Subida OTA de 142 kB por SPE | ✅ |
| Marcado `pending` y reinicio remoto | ✅ |
| Autoconfirmación tras 4 envíos SPE | ✅ `confirmed=True` |
| `tank_id` persistente tras reinicio | ✅ |
| Temperatura del ADT75 en la base de datos | ✅ |
| **Intercambio de ranuras** | ❌ ver §5 |
