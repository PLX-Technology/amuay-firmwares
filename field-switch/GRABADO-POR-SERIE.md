# Grabar el MAX32690 **por serie**, sin SWD

2026-08-03. Verificado de punta a punta en un field switch: firmware real
grabado, placa arrancando y switch configurado.

**Por qué importa:** un field switch se actualiza o se recupera con **un cable
USB**, sin depurador y sin acceso al conector SWD. Con 50 tanques en campo, la
alternativa —mandar a alguien con un MAX32625PICO y un cable de diez pines—
no escala. Además fue la única vía disponible el día que el SWD del Pico dejó
de responder.

---

## 1. La receta

```bash
# 1) Firmar como siempre (header=yes, igual que para SWD)
sign_app.exe -c MAX32690 ca=zephyr.bin sca=fw.sbin header=yes \
  algo=ecdsa key_file=<SBT>/devices/MAX32690/keys/maximtestcrk.key \
  rom_version=010203ff load_address=10000000 jump_address=<__start>

# 2) ★ RELLENAR A PAGINA COMPLETA DE 16 KB  <- el paso que falta en todas partes
python tools/sscp/pad_a_pagina.py fw.sbin fw_pad.sbin

# 3) Construir la sesion SCP
set MAXIM_SBT_DIR=C:\MaximSDK\Tools\SBT
build_scp_session.exe -c MAX32690 <dir_salida> fw_pad.sbin

# 4) Empaquetar en zip (packet.list + los .packet) y enviar
python tools/sscp/send_scp.py -c MAX32690 -s COM<pico> -i uart \
  -t 30 -e 30 -r 1 -f 900 --packet-delay 50 -v sesion.zip
```

Va por el **LPUART del conector `uC SWD` (J11), pines 6/8**, puenteado por la
CDC-UART del Pico. **No usa `SWDIO` ni `SWCLK`** — funciona con el SWD muerto.

---

## 2. ★ El relleno a pagina: la causa de que no funcionara

Sin el relleno, la transferencia llega **siempre al 99 %** y muere con:

```
error: received packet is not the expected one, module=11, code=6
       -> ERROR_MSG[K_MODULE_COMMON][6] = "ERR_FATAL_ERROR : Critical error"
```

El fallo cae exactamente en el **ultimo `write_mem`** (se ve mirando las
ultimas entradas de `packet.list`). El flash del MAX32690 se programa por
**paginas de 16 KB**, y el bloque final de una imagen sin rellenar cae a mitad
de pagina.

**La pista estaba delante todo el tiempo:** openocd, grabando esta MISMA imagen
por SWD y con exito, decia

```
wrote 638976 bytes from file mfs_rollback.sbin     <- 626812 reales
```

**638976 = 39 x 16384**. Rellenaba hasta pagina completa. Copiar eso lo
arregla:

| Imagen | Resultado |
|---|---|
| 626 812 B (sin rellenar) | **99 %**, `ERR_FATAL_ERROR` — 4 de 4 |
| **638 976 B (39 paginas)** | **100 %, `SCP session OK`** |

---

## 3. ⚠️ NO TOCAR LA PLACA DURANTE LA TRANSFERENCIA

Ni reset, ni alimentacion. **Cada interrupcion mata la sesion**, y como el
protocolo no admite reintentos (ver §5) no hay recuperacion: hay que empezar
de cero.

Esto costo veinte intentos porque **enmascaraba el fallo real**. Con
interrupciones, las sesiones morian en puntos aleatorios (3, 9, 16, 44 %) y
parecia un problema de transporte. Sin tocar nada, el comportamiento paso a ser
perfectamente repetible: 99 % siempre, mismo codigo de error.

| Ejecuciones | Resultado |
|---|---|
| Sin tocar nada | **99 %** siempre (o 100 % con el relleno) |
| Con POR o resets a media transferencia | 3, 9, 16, 44 % — aleatorio |

**Separar las dos cosas fue lo que permitio ver el patron.**

---

## 4. Cuando hace falta un POR (y cuando no)

| Estado de la placa | Que hace el ROM | Que hay que hacer |
|---|---|---|
| **Sin** aplicacion valida | Espera **indefinidamente** en el bootloader | **Nada.** Enchufar el serie y lanzar |
| **Con** aplicacion valida | Abre una ventana **breve** al arrancar | **UN** POR en frio, y despues no tocar |

⚠️ Esto corrige lo que decia `FLASHEO.md` §4, escrito para el aprovisionamiento
de la CRK en placa virgen — que es justamente el primer caso.

**Corolario operativo, y es el bueno:** recuperar un field switch "ladrillado"
en campo **no exige acertar ninguna ventana de tiempo**. Es el caso dificil y
resulta ser el mas facil.

⚠️ **Un reset por boton NO sirve si la placa se alimenta por SPE**: al
reiniciarse, el PD desaparece, el PSE corta y hay que renegociar, asi que en
realidad es un corte de alimentacion con un hueco de segundos. Para grabar,
alimentar la placa **por fuente externa**.

---

## 5. Parametros de `send_scp.py`, y por que

| Opcion | Valor | Motivo |
|---|---|---|
| `--packet-delay` | **250 ms a 24 V**, 50 ms con 50 V | Cada bloque es una escritura real en flash. Ver §5.2 |
| `-r 1` | sin reintentos | ⚠️ **NO subirlo.** SCP reproduce una secuencia grabada; reintentar un paquete que la placa ya proceso **desincroniza** la sesion (`expected data size != real one`) |
| `--connect-timeout 2` | timeout **solo al conectar** | ★ Ver §5.1. Con el `-t` largo NO se caza la ventana del ROM |
| `-t 30` | timeout por paquete | Es **por paquete**, no total. Un valor enorme solo hace que un fallo tarde minutos en verse |
| `-f 3000` | reintentos de conexion | Alarga la fase de conexion; util solo si hay que acertar la ventana |

### 5.1 ★ `--connect-timeout`: por que `-t 30` no deja conectar

2026-08-07. `-t` es el **timeout de lectura del puerto serie**, y la fase de
conexion es un bucle que manda una peticion y **espera la respuesta ese timeout
entero** antes de reintentar:

```python
for i in range(first_retry_nb):
    con_req.process(bl_scp)      # manda
    con_reply.process(bl_scp)    # espera -> timeout = -t
```

Con `-t 30` salen **dos intentos por minuto**. Y en una placa **con aplicacion
valida** el ROM abre una ventana de una **fraccion de segundo** al arrancar.
Acertarla asi es cuestion de suerte: no conecto en varios minutos y varios POR.

Con **2 s**, a la primera.

⚠️ **Por eso la receta parecia funcionar antes**: se depuro sobre placas **sin**
aplicacion valida, donde el ROM espera indefinidamente y el primer intento
conecta siempre, con el timeout que sea. El caso de la placa que arranca bien
—el normal al actualizar— nunca se habia probado.

Las dos fases piden lo contrario y antes compartian un unico `-t`: conectar
necesita **reintentar deprisa**, transferir necesita **paciencia** (un borrado de
pagina tarda mas de 2 s; con el timeout corto la sesion conecta y muere luego a
media transferencia con `timeout, no packet received`). De ahi la opcion
separada.

### 5.2 ★ A 24 V hay que CUADRUPLICAR la pausa entre paquetes

Misma fecha. Con la placa a 24 V de banco, las sesiones morian en puntos
**aleatorios**: 17 %, 19 %, 41 %, 48 %. Ese patron es el que §3 atribuye a
interrupciones — solo que aqui nadie tocaba nada.

Es el **rail hundiendose en las rafagas de escritura de flash**: conectar
consume poco y va siempre; programar da picos y la placa se reinicia.

| `--packet-delay` | Resultado a 24 V |
|---|---|
| 50 ms | muere en punto aleatorio — 4 de 4 |
| **250 ms** | **100 %, `SCP session OK`** |

Es el mismo remedio que en el flasher de la ATT (bloques de 128 B y pausa entre
ellos): dar tiempo al rail a recuperarse. **Con 50 V no hace falta**; con 24 V,
si. Y el sintoma engana, porque parece transporte.

Otras cosas comprobadas:

- **El ROM habla a 115200.** A 57600 no conecta (probado con la placa
  alimentada; un intento anterior a 57600 no valia porque estaba sin tension).
- **El puente CDC del Pico transporta bien.** Movio 322 paquetes sin perder uno.
  La teoria de que perdia bytes era falsa.
- **Usar `send_scp.py`** (el reimplemento Python en `tools/sscp/`), no el
  `send_scp.exe` del SBT.

---

## 6. Invocacion de `build_scp_session` (no documentada)

Los argumentos **posicionales** van en este orden, y no es obvio:

```
build_scp_session.exe -c MAX32690 <DIRECTORIO_SALIDA> <FICHERO>
```

El **primero** es el directorio de salida; el **segundo** es `%PARAM_1%` del
script. Pasar solo el fichero lo toma como directorio y falla con un
desconcertante `File  not found` **sin nombre**. (Fuente: `session_build.c:213`.)

- Solo acepta extensiones **`.sbin`, `.s19` o `.srec`**.
- Se puede sobreescribir cualquier opcion del `.ini` por linea de comandos con
  `clave=valor`, p.ej. `script_file=...` para escribir en otra direccion.
- La configuracion sale de `<SBT>/device_MAX32690.ini`: usa
  `write_sla.txt` (`write-file %PARAM_1% 10000000`), `chunk_size=8186` y la
  **misma `maximtestcrk.key`** con la que firmamos.

---

## 7. Descartado por el camino (para no repetirlo)

- **No es el numero de paquetes.** Con `chunk_size=15354` (178 paquetes en vez
  de 322) falla igual en el 99 %.
- **No es la cabecera de arranque.** Firmar con `header=no` —el formato del
  blinky de ejemplo del SBT, que si entra al 100 %— va **peor**, no mejor.
- **No es el tamano total.** Partir la imagen en dos mitades de ~320 KB no
  ayuda.
- **No es el transporte.** La imagen de referencia del fabricante
  (`sla/blinkled_MAX32690_EvKit_P0.14.sbin`, 38 KB) entra al 100 % siempre.

---

## 8. Comprobacion final

Tras el POR, la consola (COM del Pico, **115200**) debe mostrar el arranque
completo:

```
*** Booting Zephyr OS build ... ***
LTC4296 probe!
Check Firmware Version :: SC0000519-005-329
Configured MAC address: 00:18:80:33:c4:9a
VID 1..4 enabled on ports 0 to 5 :: 0
```

⚠️ Alimentando de banco a 24 V, el LTC4296 dira `Vin 23709V out of range,
expected 50000V to 58000V` y **no energizara ningun puerto**. Es correcto: la
clase 13 exige 50 V. Para volver a servicio, devolverle los 50 V.

De paso, ese `23709` frente a 24 000 confirma el error de ganancia del ADC
(-1,2 % aqui, -2,2 % medido en el MPS) — ver `power-switch/FLASHEO.md` §8.3.
