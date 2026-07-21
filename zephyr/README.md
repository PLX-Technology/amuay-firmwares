# Ficheros del árbol Zephyr (vendorizados)

Copia versionada de los ficheros de Zephyr que **no vienen de upstream** o que
llevan cambios nuestros, para el LTC4296 (PSE SPoE del power switch).

El árbol Zephyr de trabajo vive en `tpu01:~/zephyr` y **no está bajo control de
versiones**. Sin esta copia, reconstruirlo desde cero o clonarlo limpio pierde
el cambio de abajo, y el fallo que provoca es muy difícil de diagnosticar.

## Dónde va cada fichero

Rutas relativas a la raíz del árbol Zephyr:

| aquí | destino |
|---|---|
| `drivers/sensor/ltc4296/` | `drivers/sensor/ltc4296/` |
| `include/zephyr/drivers/sensor/ltc4296.h` | `include/zephyr/drivers/sensor/ltc4296.h` |
| `include/zephyr/dt-bindings/sensor/ltc4296.h` | `include/zephyr/dt-bindings/sensor/ltc4296.h` |
| `dts/bindings/sensor/adi,ltc4296.yaml` | `dts/bindings/sensor/adi,ltc4296.yaml` |

## CAMBIO CRÍTICO — SPI modo 3

En `drivers/sensor/ltc4296/ltc4296.c`, macro `LTC4296_DEFINE` (~línea 1283):

```c
.bus = SPI_DT_SPEC_INST_GET(inst,
    (SPI_WORD_SET(8) | SPI_OP_MODE_MASTER | SPI_TRANSFER_MSB
     | SPI_MODE_CPOL | SPI_MODE_CPHA), 0),
```

`SPI_MODE_CPOL | SPI_MODE_CPHA` (modo 3) **no está en el driver original**, que
usa modo 0. El LTC4296-1 requiere modo 3.

**Síntoma si falta:** el chip no contesta. Todos sus registros leen `0xffff` y
`device_init()` devuelve `-EINVAL`, que sale de la comprobación de desbloqueo de
`ltc4296_probe()`:

```c
ltc4296_reg_write(dev, LTC4296_REG_GCMD, LTC4296_UNLOCK_KEY);
ltc4296_reg_read(dev, LTC4296_REG_GCMD, &value);
if (value != LTC4296_UNLOCK_KEY) return -EINVAL;   /* "Device locked" */
```

Además el firmware **se traga ese error** (el `if (ret)` tras
`adin6310_enable_pse` solo hace `printf`, sin `return`), así que arranca con el
PSE muerto y toda llamada posterior cae al vacío, sin ningún aviso.

Esto nos costó días: lo interpretamos como que el LTC4296 se había dañado con un
brownout a 5,6 A. El chip estaba intacto; era el modo SPI. Con el modo 3,
`GCMD` lee `0x05` y `device_init()` devuelve 0.

## Otras notas del driver

- **SCCP lo hace el MAX32690 por software**, no el LTC4296.
  `GCAP = 0x0025` tiene el bit 6 (`SCCP_SUPPORT`) a **0**: esta variante del chip
  no trae motor SCCP interno, y por eso ADI lo bit-banguea por GPIO en
  `ltc4296_sccp.c`. El campo `NUMPORTS` (bits 4:0) = 5 confirma la numeración.
- Las primitivas de línea (`READ_LINE`, `PULL_DOWN_LINE`, `RELEASE_LINE`) usan
  `data->current_sccp_port`, que **solo se asigna dentro de `do_spoe_sccp()`**.
  Llamar a `ltc4296_sccp_reset_pulse()` por fuera actúa siempre sobre el puerto
  que quedara asignado — normalmente el 0.
- `ltc4296_port_prebias(..., LTC_CFG_APL_MODE)` activa `sig_override_good`, que
  **fabrica un `VALID_SIGNATURE` sin que haya PD**. No usar ese bit de `PXEV`
  como prueba de detección si se pasó por modo APL.
- `adi,hs-resistor` debe ser **250** (mΩ). El esquemático lleva 0,27 Ω y ADI
  documenta como válidos {3000, 1500, 680, 250, 100}. Afecta a `read_port_adc`
  y al umbral MFVS: con un valor mal puesto la corriente reportada se escala mal
  (con 27 sale ~9× inflada).

## Estado pendiente

El PSE **no entrega potencia todavía**. El chip acepta todas las órdenes
(`PxCFG0` lee de vuelta exactamente lo escrito) y tiene 54 V en `VIN`, pero
`VOUT` nunca pasa de ~70 mV y salta `OVERLOAD_DETECTED_ISLEEP`. Ocurre **igual
en puertos con un módulo PSM desnudo, sin cable ni carga**, donde no hay camino
de corriente posible: no es configuración por puerto. Pendiente de revisar con
Mayker la etapa de salida, en particular el MOSFET de lado bajo **Q18**
(`LGATE` pin 23 / `LSRC` pin 24), retorno común de los cuatro puertos.
