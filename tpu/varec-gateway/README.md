# varec-gateway

Pasarela de los sensores **Varec 2500** (tarjeta [ATT](../../att/)), corriendo en la CM5 de la TPU.

```
  ATT (encoder) ──SPE 0x88B5──▶ field switch ──▶ power switch ──▶ TPU ──┬──▶ MQTT
   × N tanques                                                          ├──▶ Modbus TCP
                                                                        ├──▶ Modbus RTU
                                                                        └──▶ HTTP/JSON
```

Recibe la medida de N tanques, la almacena en el **SSD M.2** y la reexpone por
cuatro salidas simultáneas. Cada salida se habilita por separado.

## Identidad de cada tanque

**El `tank_id` lo lleva cada ATT en su propia EEPROM y viaja en cada trama.**
La placa lleva su identidad consigo: se sustituye una ATT averiada, se le
escribe el `tank_id` por Modbus y **la pasarela no se entera**.

La alternativa —un mapa MAC→tanque en la CM5— se descartó a propósito: obliga a
editar la pasarela cada vez que se cambia una placa, y en campo eso se olvida y
acabas con el tanque 3 reportando el nivel del 7.

La MAC se guarda igualmente, para diagnóstico y para **detectar duplicados**: dos
placas con el mismo `tank_id` es un error de campo que la pasarela registra en
`id_conflicts` y grita por consola, en vez de silenciarlo.

## Almacenamiento — tres capas

Guardar cada muestra cruda de 30+ tanques durante años no es viable ni útil, así
que un proceso las consolida:

| Tabla | Contenido | Retención |
|---|---|---|
| `samples_raw` | cada muestra | días |
| `samples_1m` | media por minuto | meses |
| `samples_1h` | media por hora | **años** |

A 30 s por muestra y 30 tanques: ~3 MB/día en crudo, que consolidados a 1 h
quedan en **~11 MB/año**.

> **La base vive en el SSD M.2 (`/mnt/ssd/varec/`), nunca en la eMMC.** La eMMC
> es solo para el OS. El servicio declara `RequiresMountsFor=/mnt/ssd`: sin el
> SSD montado no arranca, en vez de escribir en el sitio equivocado.

Se usa **WAL** para que los lectores (MQTT/Modbus/HTTP) no bloqueen al escritor,
y las escrituras se **agrupan** para no hacer un commit por trama.

## Frecuencia de muestreo

**Configurable desde la propia pasarela**, sin tocar firmware: es el holding
register **`HR 4`** (`push_ms10`) de cada ATT, en decenas de ms.

```bash
# 30 s  ->  3000
mbpoll -m tcp -a <unit_id> -t 4 -r 5 <ip-att> -p 502 3000
mbpoll -m tcp -a <unit_id> -t 4 -r 10 <ip-att> -p 502 165   # 0xA5 = guardar en EEPROM
```

Un Varec 2500 mide nivel de líquido: eso se mueve en minutos, no en
milisegundos. **30 s es un punto sensato**; los 500 ms del bring-up eran para
depurar.

## Salidas

### MQTT
```
varec/<tank_id>/value      12.345
varec/<tank_id>/raw        {"tank_id":3,"value":12.345,"unit":"mm",...}
varec/gateway/status       online | offline   (con Last Will)
```
La contraseña se toma de **`VAREC_MQTT_PASS`** en el entorno, no del config.

### Modbus TCP / RTU — la pasarela es **esclavo**

Un bloque de **10 registros por tanque**, indexado por `tank_id`:

```
base = (tank_id - 1) * 10
  +0,+1   valor en unidad de ingeniería (float32, big-endian)
  +2,+3   count crudo del encoder (int32)
  +4      errores del encoder
  +5      antigüedad de la última muestra (s)
  +6      estado: bit0 = online
  +7      uptime del sensor (min)
```

Con 30 tanques son 300 registros (Modbus admite 65536). Se eligió
bloque-por-tanque en vez de un `unit_id` por tanque para que un SCADA pueda
leerlos **todos de una pasada**.

> **La TPU tiene un solo puerto RS-485.** Si se usa para *servir* al SCADA, no
> puede a la vez ser maestro de las ATT por ese bus. Por eso la ingesta va por
> SPE y el RS-485 queda libre como salida.

### HTTP / JSON
```
GET /api/tanks                          todos los tanques, último valor
GET /api/tank/<id>
GET /api/tank/<id>/history?res=raw|1m|1h
```

## Instalación

```bash
sudo mkdir -p /opt/varec-gateway /etc/varec-gateway /mnt/ssd/varec
sudo cp gateway.py outputs.py schema.sql /opt/varec-gateway/
sudo cp config.example.yaml /etc/varec-gateway/config.yaml   # y editar
sudo cp varec-gateway.service /etc/systemd/system/
sudo systemctl enable --now varec-gateway
```

Dependencias: `python3-yaml` (obligatorio), `python3-serial` (solo Modbus RTU),
`paho-mqtt` (solo MQTT). Modbus TCP/RTU y HTTP van con la biblioteca estándar.

## Notas de campo

- **La interfaz SPE se detecta por driver (`adin1110`), nunca por nombre**: los
  nombres `eth1`/`eth2`/`eth3` **se intercambian entre arranques** en esta CM5.
- **El valor actual se sirve desde RAM**, nunca del disco.
- **No conectar los dos puertos SPE de una ATT al mismo switch**: forma un lazo
  L2 y, con IP/ARP activos, la tormenta de broadcast satura el SPI bit-bang del
  `eth2` y **tumba la CM5** (comprobado, tres veces).
