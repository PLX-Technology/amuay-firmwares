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
  --connect-timeout 2 -t 30 -e 30 -r 1 -f 3000 --packet-delay 250 sesion.zip
```

★ Los dos valores que cuestan una tarde si se ponen mal (ver §5.1 y §5.2):

- **`--connect-timeout 2`** — sin esto no se caza la ventana del ROM en una
  placa que arranca bien, que es el caso normal al ACTUALIZAR.
- **`--packet-delay 250`** — si la placa esta a **24 V**. Con 50 V bastan 50 ms.

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

---

## 9. ★★ PLACA NUEVA: SON CUATRO PASOS, NO UNO

2026-08-12. Puesta en marcha de dos field switches recien fabricados. **La
receta que teniamos estaba incompleta**: creiamos que una placa nueva se
grababa y ya, y hacen falta **cuatro** pasos. El que faltaba —la CRK— nos
costo la mitad de la jornada porque las placas anteriores ya la traian de
fabrica y nunca lo habiamos visto.

| # | Que se graba | Sesion | Por que |
|---|---|---|---|
| 1 | **CRK** en el OTP | `<SBT>/devices/MAX32690/scp_packets/writemaximcrk.zip` | Sin ella el ROM no valida una imagen firmada |
| 2 | Firmware de produccion | `sesion_prod.zip` | La aplicacion |
| 3 | **Aprovisionamiento del ADIN6310** | `adin6310_provision.sbin` (rama `mps`, `power-switch/prebuilt/`) + **POR largo de ~10 s** + esperar 30 s | El switch viene EN BLANCO y el firmware de produccion no consigue cargarselo |
| 4 | Firmware de produccion **otra vez** | `sesion_prod.zip` | El paso 3 sobrescribio la aplicacion |

⚠️ **El paso 3 va SIEMPRE a 24 V, nunca a 50.** Esa imagen es anterior a las
protecciones y **energiza sin negociar**: es la que quemo dos LTC4296. A 24 V
el LTC4296 se niega a energizar y el paso es seguro.

⚠️ **El OTP es de UNA SOLA ESCRITURA.** La CRK no se puede borrar ni cambiar.
Se graba la `maximtestcrk` de fabrica, que es con la que firmamos todo.

---

## 10. ★ DIAGNOSTICO POR DONDE FALLA

Es lo mas util de todo lo aprendido: **el punto en el que muere la sesion dice
que le pasa a la placa.** Para verlo hay que capturar la salida a fichero, sin
tuberia — con `| tail` el progreso se buffea y no se ve nada.

| Sintoma | Significa | Que hacer |
|---|---|---|
| `Connection Failed`, **nunca conecta** | El ROM no contesta | Placa mala (ver §11), o **sin alimentar** — comprobarlo ANTES |
| Conecta y muere en el **1 %** | **Falta la CRK.** Corta en la autenticacion, justo tras el saludo | Paso 1 |
| Muere en un **% aleatorio** (14, 41, 67…) | El rail de 24 V se hunde en las rafagas de escritura | Reintentar; subir `--packet-delay`; o alimentar a 50 V |
| Muere **siempre al 99 %** | Imagen sin rellenar a pagina | Ver §2 |
| Llega al 100 % pero la consola repite `Firmware update in progress` | **ADIN6310 sin aprovisionar** | Pasos 3 y 4 |

**Comprobacion final de una placa buena** (§8), con CERO lineas de
`Firmware update in progress`:

```
dev_id (USN): 6b44218b1e176ce2
Check Firmware Version :: SC0000519-005-329     <- el ADIN6310 responde
Configured MAC address: 00:18:80:53:4d:69
PSE enabled
VID 1 enabled on ports 0 to 5 :: 0
```

---

## 11. ★ La tecnica del POR, corregida

§4 dice cuando hace falta un POR. Lo que faltaba es **con que ritmo**:

- **Placa virgen**: el ROM espera indefinidamente. **Engancha sola**, sin tocar
  nada, en el primer minuto.
- **⚠️ Tras una sesion FALLIDA el ROM sale de su bucle de escucha**, aunque la
  flash siga vacia. Ya no engancha sola: hay que darle un POR. Esto no estaba
  documentado y costo siete minutos de espera inutil.
- **Con firmware valido**: la ventana es un parpadeo. **Ciclar la alimentacion
  cada 30 segundos** y parar en cuanto conecte.

⚠️ **NO ciclar cada 10 segundos.** Entre que la sesion engancha, alguien lo ve
y avisa, pasan segundos: con ciclos cortos el siguiente POR cae **ya
transfiriendo** y mata la sesion. Paso tres veces seguidas. Con 30 s hay margen
de sobra.

⚠️ **Ciclar la alimentacion puede tirar el USB del Pico** (`WriteFile failed`,
`el dispositivo no reconoce el comando`). Se recupera solo; hay que relanzar.
Cortar y devolver la tension de forma limpia y no mover el cable USB.

⚠️ **No lanzar una sesion antes de que la anterior suelte el puerto**
(`could not open port: Acceso denegado`). Esperar a que el proceso termine de
verdad, no unos segundos.

---

## 12. Defecto de lote (2026-08-12) — pendiente de Mayker

De ocho placas del lote nuevo, **dos funcionaron y seis no responden al ROM**,
ni alimentadas, ni ciclando, ni por dos adaptadores serie distintos. En una se
**sustituyo el MAX32690** y siguio igual, asi que el chip no es el culpable.

**El dato que orienta la reparacion:** en una de las mudas, el `LPUART_TX` del
J11 **mantiene 3,3 V con una carga de 1 kΩ a masa**. Eso prueba que hay una
salida real empujando — el micro esta vivo, ejecutando y gobernando su
transmision. Lo que no funciona es **la recepcion**: nuestro byte no le llega.

⇒ Sospechoso unico: la linea **`LPUART_RX` entre el J11 pin 08 y la bola H10**
del micro.

**Como clasificar una placa en un minuto**, sin programador:

```
J11 pin 06 (LPUART_TX) ──[ 1 kΩ ]── masa      (comparar contra una placa buena)
```

- mantiene ~3,3 V → micro vivo, transmision buena ⇒ repasar la RX
- se desploma → nadie gobierna la linea ⇒ repasar la TX o el micro

⚠️ **Medir siempre contra una placa buena.** Y ojo con el consumo como
indicador de "micro vivo": el boton de reset esta puenteado en `SJ4` con el
reset del ADIN6310, asi que **resetea los dos chips** y la variacion de
corriente no se puede atribuir a ninguno.
