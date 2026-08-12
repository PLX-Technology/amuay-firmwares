#!/usr/bin/env python3
"""
Pasarela Varec — corre en la CM5 de la TPU.

Recibe la medida de N sensores Varec 2500 (tarjeta ATT) por SPE, la almacena
en el SSD M.2 y la reexpone por MQTT, Modbus TCP, Modbus RTU y HTTP/JSON.

    ATT (encoder) --SPE 0x88B5--> power switch --> TPU  ==> MQTT
                                                        ==> Modbus TCP/RTU
                                                        ==> HTTP/JSON

Cada tanque se identifica por el `tank_id` que la propia ATT lleva en su
EEPROM y embebe en cada trama: la placa lleva su identidad consigo, asi que
se puede sustituir una ATT averiada sin tocar la pasarela.

Arranque:  python3 gateway.py --config /etc/varec-gateway/config.yaml
"""
import argparse
import os
import queue
import signal
import socket
import sqlite3
import struct
import sys
import threading
import time
from collections import defaultdict

# ---------------------------------------------------------------- constantes
ETHERTYPE = 0x88B5
MAGIC = 0x56415245                      # "VARE"

# Trama de la ATT, big-endian. Cada version ANADE campos al final, nunca
# reordena, asi que se puede parsear por version y aceptar varias a la vez.
# Con OTA la flota estara a medio actualizar: rechazar por version seria
# tirar datos buenos.
#   v1: magic ver tank seq count edges errors uptime
#   v2: + level_mm (calibrado en la ATT) tras count
#   v3: + temp_c10 + humi_rh10 al final
FRAME_FMT_BY_VER = {
    1: "!IHHIiIII",
    2: "!IHHIiiIII",
    3: "!IHHIiiIIIhh",
    4: "!IHHIiiIIIhhH",
}
FRAME_LEN_BY_VER = {v: struct.calcsize(f) for v, f in FRAME_FMT_BY_VER.items()}
FRAME_MIN_LEN = min(FRAME_LEN_BY_VER.values())
ATT_NA = -32768          # centinela de la ATT: "sin sensor / sin dato"

# --- Telemetria del power switch (MPS): corriente por puerto del LTC4296 ---
# Ethertype propio para no mezclarla con la de los tanques.
MPS_ETHERTYPE = 0x88B6
MPS_MAGIC     = 0x4D505331          # "MPS1"
MPS_I_NA      = -32768              # el puerto no da lectura valida
#   magic ver nports seq uptime_ms  iout[4]  pxst[4]
MPS_FMT       = "!IHHII" + "h" * 4 + "H" * 4
MPS_LEN       = struct.calcsize(MPS_FMT)
#   v2, al final de la trama: gcmd unlocks vin_mv vin_ok disc_n
MPS_FMT_V2    = "!HHiHH"
MPS_LEN_V2    = struct.calcsize(MPS_FMT_V2)
#   v3, DESPUES del bloque v2: dev_id (identidad estable del USN)
MPS_FMT_V3    = "!Q"
MPS_LEN_V3    = struct.calcsize(MPS_FMT_V3)
#   v4, DESPUES de dev_id: VECINOS, gemelo del v3 del MFS. Con esto la raiz
#   del arbol dice de que SLOT cuelga cada rama; sin ello el panel enseña el
#   power switch y la rama de field switches como dos arboles sueltos.
#   vec_total(H) vec_idx0(H) vec_n(B) pad(B) + 8 x { mac[6] port pad }
MPS_VEC_N     = 8
MPS_FMT_V4    = "!HHBB"
MPS_LEN_V4    = struct.calcsize(MPS_FMT_V4) + MPS_VEC_N * 8
# Llave de desbloqueo del LTC4296 (GCMD). Con el chip bloqueado, las
# escrituras se ignoran EN SILENCIO: no hay error que mirar.
LTC_UNLOCK_KEY = 0x05
# Estado PSE (bits 2:0 de PxST). El 2 es el unico que significa "entregando".
PSE_ESTADO = {0: "deshabilitado", 1: "durmiendo", 2: "entregando",
              3: "buscando", 4: "error", 5: "inactivo", 6: "pre-deteccion",
              7: "desconocido"}

# --- Telemetria del field switch (MFS) -------------------------------------
# Mismo ethertype que el MPS: se distinguen por el magic. Un field switch tiene
# 5 puertos PSE en su LTC4296 (el MPS tiene 4).
MFS_MAGIC     = 0x4D465331          # "MFS1"
MFS_PORTS     = 5
#   magic ver nports seq uptime_ms dev_id   iout[5]  pxst[5]
MFS_FMT       = "!IHHIIQ" + "h" * MFS_PORTS + "H" * MFS_PORTS
MFS_LEN       = struct.calcsize(MFS_FMT)
# Bloque de chip identico al v2 del MPS: gcmd unlocks vin_mv vin_ok disc_n
MFS_FMT_CHIP  = MPS_FMT_V2
MFS_LEN_CHIP  = MPS_LEN_V2
#   v2, DESPUES del bloque de chip: GFLTEV (fallos globales del LTC4296)
MFS_FMT_V2    = "!H"
MFS_LEN_V2    = struct.calcsize(MFS_FMT_V2)
# BIT(0) de GFLTEV. Un puerto con esto enclavado NO vuelve a clasificar
# por mucho que se reintente, y desde fuera solo se ve "deshabilitado".
LTC_LOW_CKT_BRK = 0x0001
#   v3, DESPUES de gfltev: VECINOS. Quien se ve por cada puerto del switch.
#   vec_total(H) vec_idx0(H) vec_n(B) pad(B) + 8 x { mac[6] port pad }
MFS_VEC_N     = 8
MFS_FMT_V3    = "!HHBB"
MFS_LEN_V3    = struct.calcsize(MFS_FMT_V3) + MFS_VEC_N * 8
#   v4, DESPUES de los vecinos: la corriente SIN CONVERTIR.
#   adc_code[5] + hs_res[5], uint16 big-endian.
MFS_FMT_V4    = "!" + "H" * MFS_PORTS * 2
MFS_LEN_V4    = struct.calcsize(MFS_FMT_V4)
#   v5 del MPS: lo mismo con 4 puertos.
MPS_FMT_V5    = "!" + "H" * 4 * 2
MPS_LEN_V5    = struct.calcsize(MPS_FMT_V5)
# Lo que manda el firmware cuando el ADC no dio lectura valida (bit NEW a 0).
ADC_SIN_DATO  = 0xFFFF


def ma_exacto(code, hs_res):
    """Corriente EXACTA en mA a partir de la cuenta cruda del ADC.

    Misma cuenta que hace el driver, pero en coma flotante:

        ma = (code - 2048) * 1000 / (10 * hs_resistor)

    En C eso es una DIVISION ENTERA que trunca hacia cero. Con el shunt de
    estas placas cada cuenta vale ~0.37 mA (270, power switch) o ~0.40 mA (250,
    field switch), asi que truncar tira hasta una cuenta entera y SIEMPRE hacia
    abajo: es un sesgo sistematico, no ruido, y se acumula al sumar la potencia
    de todos los puertos.

    ⚠️ Dos decimales NO son precision de centesima de mA: el ADC no resuelve
    por debajo de una cuenta. Lo que se gana es no tirar la parte fraccionaria
    de la conversion, que si es exacta para una cuenta dada.
    """
    if code is None or code == ADC_SIN_DATO or not hs_res:
        return None
    return round((code - 2048) * 1000.0 / (10.0 * hs_res), 2)


def aplicar_ma_exacto(puertos: list, codigos, resistencias):
    """Sustituye la corriente truncada por la exacta, puerto a puerto.

    El indice coincide porque firmware y pasarela recorren los puertos del
    LTC4296 en el mismo orden.

    Se guardan tambien `adc_code` y `hs_res`: sin ellos, si algun dia una
    corriente parece rara, no habria forma de saber si el problema esta en la
    medida o en la conversion.

    ⚠️ Solo se pisa `ma` cuando hay lectura VALIDA. Si el ADC no tenia dato,
    se respeta lo que decidio el parser (None), que no es lo mismo que 0.
    """
    for i, p in enumerate(puertos):
        if i >= len(codigos) or i >= len(resistencias):
            break
        code, res = codigos[i], resistencias[i]
        p["adc_code"] = None if code == ADC_SIN_DATO else code
        p["hs_res"] = res or None
        exacto = ma_exacto(code, res)
        if exacto is not None:
            p["ma"] = exacto


# ⚠️ POR QUE LA IDENTIDAD VA DENTRO DE LA TRAMA Y NO ES LA MAC.
#
# La MAC del field switch se SORTEA EN CADA ARRANQUE
# (`field-switch/src/main.c`: srand(k_cycle_get_32()); mac_addr[3..5]=rand()).
# Indexar por MAC -- que es lo que hace el MPS hoy -- crearia un equipo nuevo
# en el panel cada vez que un switch se reinicia. Con 50 en campo, el panel se
# llenaria de fantasmas en dias.
#
# El MPS tampoco se salva por otro motivo: su MAC esta CABLEADA en el firmware
# (02:00:00:AD:4C:01), asi que dos power switches colisionarian entre si.
#
# Por eso `dev_id` es un entero de 64 bits derivado del USN del MAX32690
# (MXC_SYS_GetUSN), que es unico por chip y sobrevive a reinicios y a
# regrabados. La MAC se sigue guardando, pero solo como dato informativo.
DEV_SIN_ID = 0                      # firmware antiguo: no manda dev_id

# --- ⚠️ SERIGRAFIA DEL FIELD SWITCH: los numeros NO coinciden --------------
# De `field-switch/MAPA-SLOTS.md`, leido del esquematico:
#
#   Rotulo   datos (macPort)   potencia (LTC port)
#   Port 1        2            (uplink: recibe PoDL, no es PSE)
#   Port 2        1                  0
#   Port 3        0                  1
#   Port 4        5                  2
#   Port 5        4                  3
#   Port 6        3                  4
#
# El panel enseñaba el indice del LTC como si fuera el rotulo, con lo que
# habia un desplazamiento: la ATT conectada al "Port 4" aparecia como slot 3.
# Aqui se traduce TODO al rotulo, que es lo que el operario lee en la placa, y
# ademas permite juntar potencia (LTC) y vecinos (macPort) en la misma fila.
MFS_ROTULO_LTC = {0: "Port 2", 1: "Port 3", 2: "Port 4", 3: "Port 5", 4: "Port 6"}
MFS_ROTULO_MAC = {2: "Port 1", 1: "Port 2", 0: "Port 3", 5: "Port 4",
                  4: "Port 5", 3: "Port 6"}


# Rotulos de los macPort del MPS. Los dos extremos son seguros (del
# devicetree y de initializePorts_p): macPort 0 es el RGMII al LAN7431 -> TPU,
# macPort 5 es el RJ45 del ADIN1300.
#
# ⚠️ Los macPort 1..4 son los cuatro slots SPE, pero SU ORDEN NO SE CONOCE:
# initializePorts_p les da direcciones de PHY 5, 2, 3 y 7 — cruzadas, igual que
# en el MFS. Se dejan sin rotular a proposito en vez de suponer que macPort N
# es el slot N: un rotulo inventado que cuadre por casualidad es peor que no
# tener rotulo, porque nadie lo vuelve a mirar. Se van rellenando segun se
# comprueben contra el hardware (que slot entrega y que MAC aparece por ahi).
# macPort 4 = Slot 4: COMPROBADO en hardware el 2026-08-05, no supuesto. El
# unico slot que entregaba era el 4 (80 mA, alimentando por PDM al field switch
# en servicio) y los cuatro descendientes aparecieron todos por macPort 4.
MPS_ROTULO_MAC = {0: "TPU (RGMII)", 4: "Slot 4", 5: "RJ45"}


def puerto_de_portmap(pm: int):
    """`portMap` de la tabla dinamica es una MASCARA DE BITS, no un indice.

    Bit N = macPort N. Se vio en hardware (2026-08-05): llegaban puertos 16 y
    32, imposibles en un ADIN6310 de seis puertos. Eran los bits 4 y 5. Al
    decodificarlos, cada vecino cayo en un puerto que si estaba entregando
    potencia, que es la comprobacion que lo confirma.

    Con VARIOS bits activos la direccion se aprendio por mas de un puerto y no
    se puede atribuir a ninguno: se descarta antes que colgarla del equivocado
    y torcer el arbol.
    """
    if pm <= 0 or (pm & (pm - 1)):
        return None
    return pm.bit_length() - 1


def parse_mfs_frame(payload: bytes):
    """Decodifica la telemetria de un field switch (sin cabecera Ethernet)."""
    if len(payload) < MFS_LEN:
        return None
    f = struct.unpack(MFS_FMT, payload[:MFS_LEN])
    if f[0] != MFS_MAGIC:
        return None
    ver, nports, seq, uptime_ms, dev_id = f[1], f[2], f[3], f[4], f[5]
    base = 6
    iout = f[base:base + MFS_PORTS]
    pxst = f[base + MFS_PORTS:base + 2 * MFS_PORTS]
    puertos = []
    for i in range(min(nports, MFS_PORTS)):
        st = pxst[i] & 0x7
        puertos.append({
            "puerto": i,
            # ⚠️ La serigrafia del MFS empieza en 1 igual que la del MPS, pero
            # el mapeo NO es trivial: `MAPA-SLOTS.md` documenta que macPort5 es
            # el slot rotulado "Port 4". Aqui se expone el indice del LTC4296
            # tal cual y la correspondencia se resuelve en la UI, para no
            # enterrar un mapeo discutible dentro del parser.
            "slot": i + 1,
            "rotulo": MFS_ROTULO_LTC.get(i, "LTC %d" % i),
            "ma": None if iout[i] == MPS_I_NA else iout[i],
            "estado": PSE_ESTADO.get(st, "?"),
            "entregando": st == 2,
            "pxst": pxst[i],
        })
    d = {"tipo": "mfs", "ver": ver, "seq": seq, "uptime_s": uptime_ms // 1000,
         "dev_id": dev_id, "puertos": puertos, "chip": None}
    if len(payload) >= MFS_LEN + MFS_LEN_CHIP:
        gcmd, unlocks, vin_mv, vin_ok, disc_n = struct.unpack(
            MFS_FMT_CHIP, payload[MFS_LEN:MFS_LEN + MFS_LEN_CHIP])
        d["chip"] = {
            "gcmd": gcmd,
            "bloqueado": (gcmd & LTC_UNLOCK_KEY) != LTC_UNLOCK_KEY,
            "redesbloqueos": unlocks,
            "vin_mv": None if vin_mv < 0 else vin_mv,
            "vin_en_rango": bool(vin_ok),
            "clasif_abandonadas": disc_n,
        }
        off = MFS_LEN + MFS_LEN_CHIP
        if ver >= 2 and len(payload) >= off + MFS_LEN_V2:
            (gfltev,) = struct.unpack(MFS_FMT_V2, payload[off:off + MFS_LEN_V2])
            d["chip"]["gfltev"] = gfltev
            # Explicito porque es la diferencia entre "no hay nada conectado" y
            # "el puerto esta bloqueado y no se va a recuperar solo".
            d["chip"]["interruptor_baja"] = bool(gfltev & LTC_LOW_CKT_BRK)
            off += MFS_LEN_V2
            if ver >= 3 and len(payload) >= off + MFS_LEN_V3:
                tot, idx0, n, _ = struct.unpack(
                    MFS_FMT_V3, payload[off:off + struct.calcsize(MFS_FMT_V3)])
                b = off + struct.calcsize(MFS_FMT_V3)
                vec = []
                for i in range(min(n, MFS_VEC_N)):
                    e = payload[b + i * 8:b + i * 8 + 8]
                    p = puerto_de_portmap(e[6])
                    if p is None:
                        continue
                    vec.append({"mac": ":".join("%02x" % x for x in e[:6]),
                                "puerto": p, "portmap": e[6]})
                # ⚠️ Es un TROZO de la tabla, no la tabla entera: el switch la
                # recorre en tramas sucesivas. Quien lo consuma debe ACUMULAR.
                d["vecinos"] = {"total": tot, "idx0": idx0, "trozo": vec}
                off = b + MFS_VEC_N * 8
                if ver >= 4 and len(payload) >= off + MFS_LEN_V4:
                    v4 = struct.unpack(MFS_FMT_V4, payload[off:off + MFS_LEN_V4])
                    aplicar_ma_exacto(d["puertos"], v4[:MFS_PORTS],
                                      v4[MFS_PORTS:])
    return d


def parse_mps_frame(payload: bytes):
    """Decodifica la telemetria del MPS (sin cabecera Ethernet)."""
    if len(payload) < MPS_LEN:
        return None
    f = struct.unpack(MPS_FMT, payload[:MPS_LEN])
    if f[0] != MPS_MAGIC:
        return None
    ver, nports, seq, uptime_ms = f[1], f[2], f[3], f[4]
    iout, pxst = f[5:9], f[9:13]
    puertos = []
    for i in range(min(nports, 4)):
        st = pxst[i] & 0x7
        puertos.append({
            "puerto": i,
            "slot": i + 1,                  # serigrafia del MPS: slot = puerto + 1
            "rotulo": "Slot %d" % (i + 1),
            # None = sin lectura valida. NO es cero.
            "ma": None if iout[i] == MPS_I_NA else iout[i],
            "estado": PSE_ESTADO.get(st, "?"),
            "entregando": st == 2,
            "pxst": pxst[i],
        })
    d = {"tipo": "mps", "ver": ver, "seq": seq, "uptime_s": uptime_ms // 1000,
         "puertos": puertos, "chip": None, "dev_id": DEV_SIN_ID}
    # Solo si la version lo anuncia Y los bytes estan: asi un MPS con firmware
    # v1 sigue funcionando contra esta pasarela.
    if ver >= 2 and len(payload) >= MPS_LEN + MPS_LEN_V2:
        gcmd, unlocks, vin_mv, vin_ok, disc_n = struct.unpack(
            MPS_FMT_V2, payload[MPS_LEN:MPS_LEN + MPS_LEN_V2])
        d["chip"] = {
            "gcmd": gcmd,
            "bloqueado": (gcmd & LTC_UNLOCK_KEY) != LTC_UNLOCK_KEY,
            "redesbloqueos": unlocks,
            # -1 = aun no ha habido ninguna clasificacion que medir. None, no 0:
            # un cero aqui se leeria como "el rail esta muerto".
            "vin_mv": None if vin_mv < 0 else vin_mv,
            "vin_en_rango": bool(vin_ok),
            "clasif_abandonadas": disc_n,
        }
    # v3: identidad estable, DESPUES del bloque v2. Mismo criterio que arriba:
    # solo si la version lo anuncia Y los bytes estan, para que un MPS con
    # firmware v1 o v2 siga funcionando contra esta pasarela sin tocar nada.
    off = MPS_LEN + MPS_LEN_V2
    if ver >= 3 and len(payload) >= off + MPS_LEN_V3:
        (d["dev_id"],) = struct.unpack(MPS_FMT_V3, payload[off:off + MPS_LEN_V3])
        off += MPS_LEN_V3
        if ver >= 4 and len(payload) >= off + MPS_LEN_V4:
            tot, idx0, n, _ = struct.unpack(
                MPS_FMT_V4, payload[off:off + struct.calcsize(MPS_FMT_V4)])
            b = off + struct.calcsize(MPS_FMT_V4)
            vec = []
            for i in range(min(n, MPS_VEC_N)):
                e = payload[b + i * 8:b + i * 8 + 8]
                p = puerto_de_portmap(e[6])
                if p is None:
                    continue
                vec.append({"mac": ":".join("%02x" % x for x in e[:6]),
                            "puerto": p, "portmap": e[6]})
            # ⚠️ Es un TROZO de la tabla, no la tabla entera: el switch la
            # recorre en tramas sucesivas. Quien lo consuma debe ACUMULAR.
            d["vecinos"] = {"total": tot, "idx0": idx0, "trozo": vec}
            off = b + MPS_VEC_N * 8
            if ver >= 5 and len(payload) >= off + MPS_LEN_V5:
                v5 = struct.unpack(MPS_FMT_V5, payload[off:off + MPS_LEN_V5])
                aplicar_ma_exacto(d["puertos"], v5[:4], v5[4:])
    return d


# Ultima telemetria del MPS. En memoria a proposito: es un panel de estado
# instantaneo, no una serie historica.
PSE_LIVE = {"ts": 0, "mac": None, "puertos": [], "chip": None}
PSE_LOCK = threading.Lock()

# --- Registro de switches (power switch + field switches encadenados) -------
# Clave: identidad ESTABLE del equipo. Ver el comentario de DEV_SIN_ID sobre
# por que no puede ser la MAC.
#
# En memoria, como PSE_LIVE: es un panel instantaneo. Si algun dia hace falta
# historico de consumo por slot, va a SQLite, no aqui.
SWITCHES = {}                       # clave -> dict del equipo
SW_LOCK = threading.Lock()
# Un equipo se marca caido pasado esto, pero NO se borra: que un field switch
# desaparezca del panel es justo lo que no queremos ver en una planta.
SW_VIVO_S = 15
# Se olvida solo tras un dia sin dar senales, para que una placa retirada
# acabe desapareciendo sin intervencion.
SW_OLVIDO_S = 86400

# Momento de arranque del proceso, para poder decir la marcha de la pasarela en
# el arbol que se publica por MQTT.
ARRANQUE = time.time()


# --- Jerarquia: quien cuelga de quien ---------------------------------------
# Cada field switch reporta, a trozos, la tabla de MACs aprendidas de su switch
# y por que puerto ve cada una. Aqui se acumulan y se resuelve el arbol.
#
# ⚠️ SE ACUMULA CON CADUCIDAD, no se reemplaza: cada trama trae solo 8 entradas
# y la tabla se recorre en varias. Reemplazar dejaria el arbol parpadeando.
VEC = {}            # clave de equipo -> { mac: {"puerto": n, "ts": t} }
VEC_OLVIDO_S = 300  # una MAC no vista en 5 min se descarta


def vecinos_acumular(clave: str, trozo: list):
    t = now()
    d = VEC.setdefault(clave, {})
    for e in trozo:
        d[e["mac"]] = {"puerto": e["puerto"], "ts": t}
    for m in [m for m, v in d.items() if t - v["ts"] > VEC_OLVIDO_S]:
        del d[m]


def jerarquia(equipos: list, macs_tanque: dict) -> list:
    """Anota en cada equipo quien cuelga de sus puertos.

    `macs_tanque`: mac -> tank_id, para poder nombrar las ATT.

    ⚠️ Se resuelve por MAC porque es lo unico que el switch aprende. La
    identidad estable (dev_id) la aporta la pasarela, que ve la MAC de origen
    de cada trama de telemetria. Sin esa traduccion, un arbol construido sobre
    MACs que se sortean en cada arranque no valdria para nada.
    """
    por_mac = {e.get("mac"): e for e in equipos if e.get("mac")}
    raiz_macs = {e.get("mac") for e in equipos if e["tipo"] == "mps"}

    for e in equipos:
        vistos = VEC.get(e["clave"], {})

        # ⚠️ EL PUERTO DE SUBIDA VE TODO LO QUE HAY AGUAS ARRIBA.
        #
        # La tabla de direcciones del switch NO distingue arriba de abajo: por
        # el puerto por el que sube, este equipo ve el power switch, la TPU y
        # todos los tanques de las demas ramas. Tomar eso por "hijos" invierte
        # el arbol -- un field switch de banco llego a declararse padre del
        # power switch (2026-08-05).
        #
        # El puerto de subida es aquel por el que se ve la RAIZ (el power
        # switch). Si no se ve ninguna, se usa el puerto con mas direcciones:
        # el de subida agrega todo lo de arriba, asi que casi siempre gana.
        #
        # ⚠️ EL POWER SWITCH NO TIENE SUBIDA: es la raiz. Y para el NO vale la
        # heuristica del "puerto con mas direcciones": por su slot 4 ve los dos
        # field switches y sus tres tanques, mientras que por el RGMII solo ve
        # a la TPU. La heuristica elegiria el slot 4 y borraria de un plumazo
        # la rama entera, que es EXACTAMENTE la inversion que ya se corrigio en
        # los field switches.
        es_raiz = e["tipo"] == "mps"
        subida = None
        for mac, v in vistos.items():
            if mac in raiz_macs and not es_raiz:
                subida = v["puerto"]
                break
        if es_raiz:
            vistos = dict(vistos)   # la raiz no descarta nada por subida
        if subida is None and vistos and not es_raiz:
            cuenta = {}
            for v in vistos.values():
                cuenta[v["puerto"]] = cuenta.get(v["puerto"], 0) + 1
            if cuenta:
                mx = max(cuenta.values())
                # Solo si destaca de verdad; con empate no se adivina.
                cands = [p for p, c in cuenta.items() if c == mx]
                if len(cands) == 1 and mx > 1:
                    subida = cands[0]
        e["puerto_subida"] = subida

        hijos = {}
        for mac, v in vistos.items():
            if subida is not None and v["puerto"] == subida:
                continue        # aguas arriba: no es hijo
            otro = por_mac.get(mac)
            if otro is not None and otro is not e:
                q = ("switch", otro["clave"], otro["tipo"])
            elif mac in macs_tanque:
                q = ("tanque", macs_tanque[mac], None)
            else:
                continue
            mapa = MFS_ROTULO_MAC if e["tipo"] == "mfs" else MPS_ROTULO_MAC
            rot = mapa.get(v["puerto"], "macPort %d" % v["puerto"])
            hijos.setdefault(rot, []).append(
                {"tipo": q[0], "id": q[1], "clase": q[2], "mac": mac})
        e["hijos_por_puerto"] = dict(sorted(hijos.items()))
        e["n_vecinos"] = len(vistos)

    # ⚠️ ANIDAR DE VERDAD: quitar de cada equipo lo que ya cuelga de un switch
    # suyo. La tabla de direcciones ve TODO lo que hay detras de un puerto, no
    # solo al vecino inmediato: el power switch lista por su slot 4 los DOS
    # field switches Y los tres tanques (comprobado en hardware, 2026-08-05).
    # Sin esta poda cada tanque sale dos veces --colgando del MPS y de su field
    # switch-- y eso no es un arbol anidado, es una lista con sangria.
    #
    # `bajo[S]` = todo lo que S ve por puertos que no son el de subida, o sea
    # todo lo que tiene detras, ya transitivo: por eso basta una pasada.
    bajo = {}
    for e in equipos:
        sub = e.get("puerto_subida")
        bajo[e["clave"]] = {m for m, v in VEC.get(e["clave"], {}).items()
                            if sub is None or v["puerto"] != sub}
    for e in equipos:
        for rot, lst in list(e.get("hijos_por_puerto", {}).items()):
            detras = set()
            for h in lst:
                if h["tipo"] == "switch":
                    detras |= bajo.get(h["id"], set())
            if not detras:
                continue
            queda = [h for h in lst if h["mac"] not in detras]
            # Si la poda se lo llevaria TODO, no se poda: eso solo pasa si dos
            # equipos del mismo puerto se ven el uno al otro (un lazo), y en
            # ese caso es mejor enseñar de mas que dejar el puerto vacio.
            if queda:
                e["hijos_por_puerto"][rot] = queda

    # Padre = el equipo que ve a este por alguno de sus puertos.
    for e in equipos:
        e["padre"] = None
        for otro in equipos:
            if otro is e:
                continue
            for lst in otro.get("hijos_por_puerto", {}).values():
                if any(h["tipo"] == "switch" and h["id"] == e["clave"] for h in lst):
                    e["padre"] = otro["clave"]
    return equipos


def sw_clave(tipo: str, dev_id: int, mac: str) -> str:
    """Identidad del equipo en el registro.

    Con `dev_id` (firmware nuevo) la identidad es estable de por vida. Sin el,
    se cae a la MAC y se marca `id_estable=False` para que el panel pueda
    avisar: en un field switch antiguo esa MAC cambia en cada arranque.
    """
    if dev_id and dev_id != DEV_SIN_ID:
        return f"{tipo}:{dev_id:016x}"
    return f"{tipo}:mac:{mac}"


def registrar_switch(tipo: str, mac: str, fr: dict):
    """Guarda/actualiza la telemetria de un switch en el registro."""
    dev_id = fr.get("dev_id", DEV_SIN_ID)
    clave = sw_clave(tipo, dev_id, mac)
    with SW_LOCK:
        eq = SWITCHES.get(clave)
        if eq is None:
            eq = SWITCHES[clave] = {"clave": clave, "tipo": tipo,
                                    "visto_primero": now()}
        eq["ts"] = now()
        eq["mac"] = mac
        eq["dev_id"] = dev_id
        eq["id_estable"] = bool(dev_id and dev_id != DEV_SIN_ID)
        eq["puertos"] = fr["puertos"]
        eq["chip"] = fr.get("chip")
        eq["uptime_s"] = fr["uptime_s"]
        eq["seq"] = fr["seq"]
        eq["ver"] = fr.get("ver")
    v = fr.get("vecinos")
    if v:
        vecinos_acumular(clave, v.get("trozo") or [])


def _potencia_por_slot(eq: dict):
    """Rellena `w` por slot y `w_total`. Comparte criterio con pse_snapshot():
    sin corriente o sin tension el resultado es None, NUNCA 0.0."""
    vin_mv = (eq.get("chip") or {}).get("vin_mv")
    total = None
    for p in eq["puertos"]:
        ma = p.get("ma")
        if vin_mv is None or ma is None:
            p["w"] = None
        else:
            p["w"] = round(vin_mv * ma / 1e6, 3)
            total = (total or 0.0) + p["w"]
    eq["w_total"] = None if total is None else round(total, 3)
    eq["w_vin_mv"] = vin_mv


def switches_snapshot(macs_tanque: dict = None) -> list:
    """Lista de switches conocidos, ordenada, con potencia y jerarquia.

    `macs_tanque` (mac -> tank_id) permite nombrar las ATT en el arbol. Se pasa
    desde la capa HTTP, que es la que tiene el estado vivo de los tanques.
    """
    t = now()
    with SW_LOCK:
        for clave in [k for k, v in SWITCHES.items()
                      if t - v.get("ts", 0) > SW_OLVIDO_S]:
            del SWITCHES[clave]
        equipos = []
        for eq in SWITCHES.values():
            c = dict(eq)
            c["puertos"] = [dict(p) for p in eq.get("puertos", [])]
            equipos.append(c)
    for eq in equipos:
        eq["edad_s"] = t - eq["ts"] if eq.get("ts") else None
        eq["vivo"] = eq["edad_s"] is not None and eq["edad_s"] <= SW_VIVO_S
        _potencia_por_slot(eq)
    # El power switch primero: es la raiz de la cadena de alimentacion.
    equipos.sort(key=lambda e: (e["tipo"] != "mps", e.get("dev_id") or 0,
                                e.get("mac") or ""))
    return jerarquia(equipos, macs_tanque or {})


# ======================================================== arbol para publicar
# Version del esquema del documento que se publica por MQTT. MISMA CONVENCION
# que las tramas de las placas: cada version ANADE campos y jamas reordena ni
# quita, para que un consumidor viejo siga funcionando. Si algun dia hay que
# romperla de verdad, se sube este numero y se avisa.
ARBOL_ESQUEMA = 1

# Nombres legibles por identificador de equipo, del config.yaml. Con 50 tanques
# nadie lee "mfs:881e316fef38f712" en una pantalla.
#
# ⚠️ Se indexa por el ID ESTABLE (el dev_id del USN del micro), no por MAC ni
# por posicion: es lo unico que sobrevive a un reinicio y a un recableado. Un
# alias colgado de la MAC de un field switch se despegaria solo, porque esa MAC
# se sortea en cada arranque.
#
# `id` NUNCA cambia en el documento publicado: el alias va en `nombre`. Asi un
# consumidor correlaciona por `id` y enseña `nombre`, y renombrar un equipo no
# le rompe el historico.
ALIAS = {}


# Milimetros de cinta por pulso del encoder, para un cabezal Varec 2500 en
# configuracion INGLESA (dos ruedas contadoras y cuadrante de pulgadas/16avos).
#
# De donde sale: el manual del 2500 (IOM001, rev I) NO publica el perimetro del
# piñon ni el paso de la cinta perforada -- se busco entero. Pero si publica la
# escala del cuadrante, que cuelga del mismo eje (p. 56-57):
#
#   inglesa : rueda derecha = 1 ft, cuadrante = 12 pulgadas -> 304,8 mm/vuelta
#   metrica : rueda derecha = 0,1 m, cuadrante = 10x10 mm   -> 100,0 mm/vuelta
#
# y el encoder da 512 cuentas por vuelta (128 PPR en cuadratura x4):
#
#   inglesa : 304,8 / 512 = 0,595312 mm/pulso
#   metrica : 100,0 / 512 = 0,195313 mm/pulso
#
# ⚠️ Esto vale SI el encoder va al eje del cuadrante. Es un acople mecanico
# nuestro, no de Varec, asi que el valor se ofrece como DEFECTO, no como
# verdad: el formulario deja escribirlo, y la forma honesta de confirmarlo en
# campo es mover la cinta con la perilla de comprobacion y dividir el
# desplazamiento leido en el contador mecanico entre los pulsos contados.
MM_POR_PULSO_INGLESA = 304.8 / 512.0
MM_POR_PULSO_METRICA = 100.0 / 512.0


def aplicar_calibracion(store, live, orden: dict) -> dict:
    """Aplica una calibracion y devuelve el resultado.

    Dos formas de decir la misma recta `nivel = pulsos*escala + offset`:

      modo "dos_puntos" (por defecto) -- dos parejas (pulsos, nivel) medidas.
          Es lo exacto, pero exige MOVER el flotador entre dos niveles
          conocidos, y en un tanque en servicio eso significa mover producto.

      modo "geometrica" -- `mm_por_pulso` (constante mecanica del cabezal, ver
          MM_POR_PULSO_INGLESA) y `altura_referencia` (la cota del cero del
          contador, medible con cinta metrica). De ahi:
              escala = sentido * mm_por_pulso      (sentido -1 por defecto)
              offset = altura_referencia
          que es exactamente `nivel = altura_referencia - pulsos*mm_por_pulso`.
          No hay que mover nada, y por eso es la unica via practica para dar de
          alta 50 tanques.

    No se guarda `mm_por_pulso` ni `altura_referencia` aparte: son |escala| y
    offset, se recuperan tal cual de la recta. Una copia mas seria una copia
    mas que puede discrepar.

    ★ LA TPU ES LA UNICA QUE ESCRIBE. La sala de visualizacion manda la orden,
    esta funcion la aplica y el arbol retenido republica la calibracion
    vigente. Asi no hay dos copias del dato que puedan discrepar: la sala
    muestra lo que la TPU confirma, y si se reinicia, o hay dos salas, o
    alguien calibra desde el panel, todas ven lo mismo sin sincronizar nada.

    Devuelve SIEMPRE un dict con `estado` y, si se rechaza, el `motivo`. Sin
    respuesta explicita el emisor no puede distinguir "no llego" de "llego y
    fallo", que son dos averias muy distintas.
    """
    res = {"id": orden.get("id"), "ts": now()}
    modo = (orden.get("modo") or "dos_puntos").strip().lower()
    res["modo"] = modo
    try:
        tid = int(orden["tank_id"])
    except (KeyError, TypeError, ValueError) as e:
        res.update(estado="rechazada", motivo="orden mal formada: %s" % e)
        return res
    res["tank_id"] = tid

    if not store.disponible:
        # ⚠️ Sin base, set_cal() no guarda nada. Aceptar la orden aqui seria
        # decirle a la sala que quedo calibrado cuando se perderia al reiniciar.
        res.update(estado="rechazada",
                   motivo="sin almacenamiento: la calibracion no se podria guardar")
        return res

    aviso = None
    if modo == "geometrica":
        try:
            mmp = float(orden["mm_por_pulso"])
            href = float(orden["altura_referencia"])
        except (KeyError, TypeError, ValueError) as e:
            res.update(estado="rechazada",
                       motivo="orden mal formada: faltan mm_por_pulso y/o "
                              "altura_referencia (%s)" % e)
            return res
        if mmp <= 0:
            res.update(estado="rechazada",
                       motivo="mm_por_pulso debe ser > 0; el sentido va aparte")
            return res
        # `sentido` es hacia donde va el NIVEL cuando los pulsos suben. Por
        # defecto -1, que es la geometria de flotador y cinta: sube el nivel,
        # el flotador sube, la cinta se recoge y la cuenta baja. Se admite
        # numero o palabra porque la sala manda JSON escrito por humanos.
        s = orden.get("sentido", -1)
        if isinstance(s, str):
            s = {"baja": -1, "bajar": -1, "sube": 1, "subir": 1}.get(s.strip().lower())
            if s is None:
                res.update(estado="rechazada",
                           motivo="sentido debe ser 'baja'/'sube' o -1/+1")
                return res
        s = -1 if float(s) < 0 else 1
        escala = s * mmp
        offset = href
        # Un cabezal 2500 mide como mucho 60 ft (18,2 m), o 90 ft (27,4 m) en
        # rango extendido; y un tanque de menos de 30 cm no existe. Fuera de
        # esa horquilla casi siempre es la unidad equivocada -- metros donde se
        # esperaban milimetros, que da un nivel 1000 veces menor y creible.
        # No se rechaza (la unidad la elige el usuario) pero se dice.
        if not (300 <= href <= 30000):
            aviso = ("altura_referencia = %g: fuera del rango util de un 2500 "
                     "(0,3 a 27,4 m). ¿Esta en las unidades correctas?" % href)
    else:
        try:
            a, b = orden["punto_a"], orden["punto_b"]
            ca, la = float(a["pulsos"]), float(a["nivel"])
            cb, lb = float(b["pulsos"]), float(b["nivel"])
        except (KeyError, TypeError, ValueError) as e:
            res.update(estado="rechazada", motivo="orden mal formada: %s" % e)
            return res

        if cb == ca:
            res.update(estado="rechazada",
                       motivo="los dos puntos tienen los mismos pulsos: no definen una recta")
            return res

        escala = (lb - la) / (cb - ca)
        offset = la - ca * escala
        if escala == 0.0:
            res.update(estado="rechazada",
                       motivo="los dos puntos dan el mismo nivel: la recta seria plana")
            return res

        # Separacion corta = error amplificado. No se rechaza (puede ser
        # deliberado en un tanque pequeño) pero se dice.
        if abs(cb - ca) < 100:
            aviso = ("puntos muy juntos (%d pulsos): el error de medida se "
                     "amplifica; separalos todo lo que puedas" % abs(cb - ca))

    store.set_cal(tid, escala, offset, orden.get("unidad") or "mm")
    res.update(estado="aplicada", escala=escala, offset=offset,
               unidad=orden.get("unidad") or "mm")
    if aviso:
        res["aviso"] = aviso
    return res


def _nodo_tanque(rec: dict) -> dict:
    """Hoja del arbol: una ATT sobre su tanque."""
    avisos = []
    if not rec.get("online"):
        avisos.append("sin_telemetria")
    # ⚠️ `value` es None cuando la ATT no tiene referencia de calibracion. Se
    # publica null y se avisa, en vez de mandar el crudo como si fuera un nivel.
    if rec.get("value") is None or not rec.get("ref_ok", True):
        avisos.append("sin_referencia")
    if rec.get("errors"):
        avisos.append("errores_encoder")
    tid = "tanque:%s" % rec.get("tank_id")
    # Recta vigente. Escala 1 y offset 0 es la identidad: el "nivel" que se
    # publica son PULSOS, no milimetros.
    esc = float(rec.get("scale") or 1.0)
    off = float(rec.get("offset") or 0.0)
    calibrado = not (esc == 1.0 and off == 0.0)
    return {
        "tipo": "tanque",
        "id": tid,
        "nombre": ALIAS.get(tid) or rec.get("name"),
        "tank_id": rec.get("tank_id"),
        "mac": rec.get("mac"),
        "vivo": bool(rec.get("online")),
        "edad_s": rec.get("age_s"),
        "uptime_s": rec.get("uptime_s"),
        "nivel": rec.get("value"),          # null si no hay referencia
        "unidad": rec.get("unit"),
        "ref_ok": rec.get("ref_ok"),
        "pulsos": rec.get("count"),
        "flancos": rec.get("edges"),
        "errores": rec.get("errors"),
        "temp_c": rec.get("temp_c"),
        "humi_rh": rec.get("humi_rh"),
        # ★ CALIBRACION VIGENTE, para que la sala de visualizacion no guarde
        # copia propia: muestra lo que la TPU confirma. Con una sola copia del
        # dato, dos plataformas no pueden discrepar -- no hay nada que
        # sincronizar porque no hay dos versiones.
        "calibracion": {
            # ⚠️⚠️ SIN CALIBRAR, **TODOS** LOS NUMEROS VAN A null.
            #
            # Escala 1 y offset 0 es la recta identidad: significa que el
            # "nivel" que se publica son PULSOS, no milimetros. Es cierto
            # matematicamente y una trampa en la practica -- 1,0 mm/pulso
            # tiene toda la pinta de una geometria real, y quien pinte el
            # campo sin mirar `calibrado` enseñaria un numero inventado.
            # Un null no se puede confundir con una medida.
            #
            # ⚠️ Y VAN TODOS, NO SOLO ALGUNOS. Hasta el 2026-08-11 los tres
            # derivados iban a null pero `escala` y `offset` seguian saliendo
            # con 1,0 y 0,0: dentro del MISMO objeto, unos campos decian la
            # verdad y otros no. Lo reporto Gabriel desde la plataforma web,
            # que acabo apoyandose solo en `calibrado` porque era el unico
            # campo fiable en los dos casos. Si un campo miente, el consumidor
            # deja de fiarse del objeto entero -- y con razon.
            "escala": esc if calibrado else None,
            "offset": off if calibrado else None,
            # La unidad tambien: sin recta, el numero publicado no esta en
            # milimetros ni en nada, son cuentas del encoder.
            "unidad": rec.get("unit") if calibrado else None,
            "calibrado": calibrado,
            # La misma recta leida en terminos fisicos, que es como se calibra
            # en campo: cuanto avanza la cinta por pulso, y a que cota esta el
            # cero del contador. No es un dato aparte -- se deriva de la recta,
            # asi que no puede desincronizarse de ella.
            "mm_por_pulso": abs(esc) if calibrado else None,
            "altura_referencia": off if calibrado else None,
            # -1: suben los pulsos, baja el nivel (flotador y cinta).
            "sentido": (-1 if esc < 0 else 1) if calibrado else None,
        },
        "avisos": avisos,
        "puertos": [],                      # una ATT es hoja: nunca tiene hijos
    }


def _avisos_switch(eq: dict) -> list:
    """Problemas del equipo en texto fijo, para que el consumidor no tenga que
    re-deducirlos de los registros crudos del LTC4296."""
    av = []
    if not eq.get("vivo"):
        av.append("sin_telemetria")
    ch = eq.get("chip") or {}
    if ch.get("bloqueado"):
        av.append("chip_bloqueado")
    if ch.get("interruptor_baja"):
        # GFLTEV bit 0 enclavado: ese equipo NO vuelve a clasificar solo.
        av.append("interruptor_baja")
    if ch.get("vin_mv") is not None and not ch.get("vin_en_rango", True):
        av.append("vin_fuera_de_rango")
    if not eq.get("id_estable", True):
        # Firmware viejo sin dev_id: su MAC se sortea en cada arranque, asi que
        # su identidad en este arbol NO es de fiar entre reinicios.
        av.append("id_no_estable")
    return av


def _nodo_switch(eq: dict, por_clave: dict, tanques: dict, vistos: set) -> dict:
    """Nodo de un power switch o field switch, con sus puertos y lo que cuelga.

    `vistos` corta un ciclo si la topologia llegara mal: un equipo no puede
    aparecer dos veces en la misma rama.
    """
    hijos_por_rotulo = eq.get("hijos_por_puerto") or {}
    puertos = []
    usados = set()

    def _cuelga(rotulo):
        """Nodos que cuelgan de un rotulo, resolviendo switches y tanques."""
        out = []
        for h in hijos_por_rotulo.get(rotulo, []):
            if h["tipo"] == "switch":
                otro = por_clave.get(h["id"])
                if otro is not None and h["id"] not in vistos:
                    out.append(_nodo_switch(otro, por_clave, tanques,
                                            vistos | {h["id"]}))
            else:
                rec = tanques.get(h["id"])
                if rec is not None:
                    out.append(_nodo_tanque(rec))
        return out

    # Primero los puertos PSE: salen SIEMPRE, tengan algo o no. Un slot vacio
    # es informacion -- distingue "no hay nada" de "hay algo y no arranca".
    for p in eq.get("puertos", []):
        rot = p.get("rotulo")
        usados.add(rot)
        puertos.append({
            "rotulo": rot,
            "estado": p.get("estado"),
            "entregando": p.get("entregando"),
            "ma": p.get("ma"),
            "w": p.get("w"),
            # Trazabilidad de la medida: si una corriente parece rara, esto
            # dice si el problema esta en el ADC o en la conversion.
            "adc_code": p.get("adc_code"),
            "hs_res": p.get("hs_res"),
            "hijos": _cuelga(rot),
        })
    # ...y luego los puertos SIN PSE por los que se ve algo (el de subida, el
    # RJ45, el RGMII). Si no se anadieran, un field switch encadenado por un
    # puerto sin PSE DESAPARECERIA del arbol.
    for rot in hijos_por_rotulo:
        if rot in usados:
            continue
        puertos.append({
            "rotulo": rot,
            "estado": "sin PSE",
            "entregando": False,
            "ma": None,
            "w": None,
            "hijos": _cuelga(rot),
        })

    return {
        "tipo": eq.get("tipo"),
        "id": eq.get("clave"),
        "nombre": ALIAS.get(eq.get("clave")) or eq.get("clave"),
        "mac": eq.get("mac"),
        "vivo": eq.get("vivo"),
        "edad_s": eq.get("edad_s"),
        "uptime_s": eq.get("uptime_s"),
        "ver_trama": eq.get("ver"),
        "id_estable": eq.get("id_estable"),
        "w_total": eq.get("w_total"),
        "vin_mv": (eq.get("chip") or {}).get("vin_mv"),
        "puerto_subida": eq.get("puerto_subida"),
        "avisos": _avisos_switch(eq),
        "puertos": puertos,
    }


def _hwmon(nombre: str, fichero: str = "temp1_input"):
    """Lee un valor de hwmon por NOMBRE de dispositivo, no por numero.

    ⚠️ Los hwmonN se renumeran entre arranques segun el orden en que registran
    los drivers; atarse a hwmon1 daria la temperatura del chip equivocado sin
    avisar. Por eso se busca por `name`.
    """
    try:
        for d in sorted(os.listdir("/sys/class/hwmon")):
            base = "/sys/class/hwmon/" + d
            try:
                with open(base + "/name") as f:
                    if f.read().strip() != nombre:
                        continue
                with open(base + "/" + fichero) as f:
                    return int(f.read().strip())
            except OSError:
                continue
    except OSError:
        pass
    return None


def _uptime_maquina():
    """Segundos que lleva encendida la TPU, no el proceso.

    De /proc/uptime y no de `time.time() - arranque del proceso`: son cosas
    distintas y la que interesa aqui es la de la maquina. Un despliegue
    reinicia el proceso; solo un corte de luz o un reinicio del sistema pone
    esta a cero, y eso es justo lo que hay que poder ver despues.

    Es tambien inmune al reloj: /proc/uptime lo lleva el nucleo, asi que un
    salto de NTP no lo altera. Con `time.time()` un ajuste de hora daria una
    marcha falsa, o negativa.
    """
    try:
        with open("/proc/uptime") as f:
            return int(float(f.read().split()[0]))
    except (OSError, ValueError, IndexError):
        return None


def _red_iface(nombre: str) -> dict:
    """Direccion IPv4, MAC y estado de enlace de una interfaz.

    Se usa el ioctl SIOCGIFADDR en vez de lanzar `ip addr`: esto se llama en
    cada foto del arbol (cada 10 s) y no merece un proceso nuevo cada vez.

    `ip` a None significa SIN DIRECCION, que es un estado legitimo: eth0 se
    dejo sin IP a proposito para el switch industrial de planta.
    """
    ip = None
    try:
        import fcntl
        s_ = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            ip = socket.inet_ntoa(fcntl.ioctl(
                s_.fileno(), 0x8915,                      # SIOCGIFADDR
                struct.pack("256s", nombre[:15].encode()))[20:24])
        except OSError:
            ip = None
        finally:
            s_.close()
    except Exception:
        ip = None

    def leer(f):
        try:
            with open("/sys/class/net/%s/%s" % (nombre, f)) as fh:
                return fh.read().strip()
        except OSError:
            return None

    vel = leer("speed")
    return {"ip": ip, "mac": leer("address"),
            "enlace": leer("carrier") == "1",
            "mbps": int(vel) if (vel or "").lstrip("-").isdigit() and int(vel) > 0 else None}


def tpu_salud() -> dict:
    """Estado fisico de la propia TPU, para publicarlo junto al resto.

    Hasta ahora el arbol contaba el estado del CAMPO pero no el del equipo que
    lo vigila. La temperatura del SoC y la del NVMe importan de verdad aqui: el
    disco se ha caido dos veces en un dia y el calor es uno de los sospechosos,
    asi que conviene tener la serie y no solo la anecdota.

    `ambiente` queda preparado para el sensor de temperatura y humedad de la
    TPU; hoy vale None porque ese sensor NO esta declarado en el sistema (sin
    overlay, sin IIO y con los buses i2c vacios).
    """
    def c(v):
        return None if v is None else round(v / 1000.0, 1)

    # ⚠️ `rpi_volt` NO da una tension: expone in0_lcrit_alarm, la alarma de
    # SUBTENSION del firmware (0 = bien, 1 = la fuente no da lo suficiente). Los
    # in1..in4 del rp1_adc son canales del conversor analogico, no la
    # alimentacion -- confundirlos fue un error mio al montar esto.
    sub = _hwmon("rpi_volt", "in0_lcrit_alarm")
    return {
        # ⚠️ DOS marchas distintas, y confundirlas despista de verdad:
        #   `uptime_s`  = la MAQUINA. Se reinicia con un corte de luz o un
        #                 reinicio del sistema. Es el que delata que la TPU se
        #                 fue y volvio sin que nadie se enterara.
        #   `pasarela_s`= el PROCESO. Se reinicia ademas con cada despliegue y
        #                 cada vez que systemd lo levanta tras un fallo.
        # Que el proceso lleve minutos y la maquina dias es normal (un
        # despliegue). Al reves es imposible; y que los dos se pongan a cero a
        # la vez es un corte de alimentacion.
        "uptime_s": _uptime_maquina(),
        "pasarela_s": int(time.time() - ARRANQUE),
        "soc_c": c(_hwmon("cpu_thermal")),
        "nvme_c": c(_hwmon("nvme")),
        "rp1_c": c(_hwmon("rp1_adc")),
        "ventilador_rpm": _hwmon("pwmfan", "fan1_input"),
        "subtension": None if sub is None else bool(sub),
        # Sensor de ambiente de la TPU: pendiente de identificar y declarar.
        "ambiente": {"temp_c": None, "humi_rh": None},
        # Red. `eth0` es el puerto de PLANTA: hoy sin IP a proposito, la
        # recibira del switch industrial. La MAC es la que hay que reservar
        # ahi para que el MQTT y el panel tengan siempre la misma direccion.
        "red": {n: _red_iface(n) for n in ("eth0", "pcie0", "wlan0")},
    }


def arbol(equipos: list, tanques: dict, almacen: dict = None,
          nuevas: list = None) -> dict:
    """Documento JERARQUICO de toda la instalacion, para publicar por MQTT.

    TPU -> power switch -> field switches encadenados -> tanques.

    `equipos` es lo que devuelve switches_snapshot() (ya trae la jerarquia
    resuelta) y `tanques` el snapshot vivo, {tank_id: registro}.

    ⚠️ La forma la fija ARBOL_ESQUEMA y solo CRECE. Quien consuma esto no
    deberia asumir que conoce todos los campos, solo los que necesita.
    """
    por_clave = {e["clave"]: e for e in equipos}
    # Un tanque puede colgar de un field switch por su tank_id; la jerarquia
    # los identifica por id, no por MAC.
    por_tanque = {r.get("tank_id"): r for r in (tanques or {}).values()}

    # Raiz de la cadena = equipo sin padre. Normalmente el power switch; si un
    # field switch aparece suelto (aun sin resolver de quien cuelga) tambien
    # sale aqui, colgado de la TPU, que es donde de verdad se le ve.
    raices = [e for e in equipos if not e.get("padre")]
    nodos = [_nodo_switch(e, por_clave, por_tanque, {e["clave"]})
             for e in raices]

    # ⚠️ TANQUES SIN UBICAR. Un tanque caido hace rato ya no esta en la tabla de
    # direcciones de ningun switch, asi que no cuelga de ninguna rama y
    # DESAPARECERIA del arbol -- justo lo que un sistema de monitoreo no puede
    # hacer: que lo que se cae se vuelva invisible. Se sabe que existen, no se
    # sabe donde cuelgan, y eso es lo que se dice. (Visto con tank24 el
    # 2026-08-05: 8 dias caido y fuera del arbol, pero contado en el resumen.)
    colocados = set()

    def _recoger(n):
        if n.get("tipo") == "tanque":
            colocados.add(n.get("tank_id"))
        for p in n.get("puertos", []):
            for h in p.get("hijos", []):
                _recoger(h)

    for n in nodos:
        _recoger(n)
    huerfanos = [_nodo_tanque(r) for tid, r in sorted((por_tanque or {}).items(),
                                                      key=lambda kv: kv[0])
                 if tid not in colocados]
    for h in huerfanos:
        h["avisos"] = list(h["avisos"]) + ["sin_ubicar"]

    n_tanques = sum(1 for r in (tanques or {}).values())
    en_linea = sum(1 for r in (tanques or {}).values() if r.get("online"))
    doc = {
        "esquema": ARBOL_ESQUEMA,
        "ts": now(),
        "generado_por": "varec-gateway",
        # ★ Salud del historiador. Va en el documento porque la primera vez que
        # el SSD se cayo fue un fallo SILENCIOSO: los niveles seguian llegando y
        # el panel se veia normal mientras no se guardaba nada.
        "almacenamiento": almacen or {"disponible": None},
        # Estado fisico de la propia TPU. Ver tpu_salud().
        "tpu": tpu_salud(),
        "resumen": {
            "equipos": len(equipos),
            "equipos_vivos": sum(1 for e in equipos if e.get("vivo")),
            "tanques": n_tanques,
            "tanques_en_linea": en_linea,
            "tanques_sin_ubicar": len(huerfanos),
            "placas_sin_asignar": len(nuevas or []),
        },
        # ★ Placas conectadas y transmitiendo pero SIN numero de tanque. No son
        # tanques -- por eso van fuera de la jerarquia y no cuentan en
        # `tanques` -- pero se publican para que la sala pueda darlas de alta
        # igual que el panel. Una placa nueva que no se ve es una placa que
        # alguien va a ir a revisar al campo sin necesidad.
        "sin_asignar": [{
            "tipo": "placa_sin_asignar",
            "mac": r.get("mac"),
            "vivo": bool(r.get("online")),
            "edad_s": r.get("age_s"),
            "uptime_s": r.get("uptime_s"),
            "pulsos": r.get("count"),
            "errores": r.get("errors"),
            "temp_c": r.get("temp_c"),
            "humi_rh": r.get("humi_rh"),
            "ver": r.get("ver"),
            "avisos": ["sin_identidad"],
        } for r in (nuevas or [])],
        "raiz": {
            "tipo": "tpu",
            "id": "tpu:%s" % socket.gethostname(),
            "nombre": (ALIAS.get("tpu:%s" % socket.gethostname())
                       or socket.gethostname()),
            "mac": None,
            "vivo": True,           # si no lo estuviera, esto no se publicaria
            "edad_s": 0,
            "uptime_s": int(time.time() - ARRANQUE),
            "avisos": ([] if (almacen or {}).get("disponible", True)
                       else ["sin_almacenamiento"]),
            # Un solo puerto logico: la TPU ve la red SPE por el RGMII del
            # power switch. Los equipos sin padre resuelto cuelgan aqui.
            "puertos": [{
                "rotulo": "SPE",
                "estado": "enlace",
                "entregando": None,
                "ma": None,
                "w": None,
                "hijos": nodos,
            }] + ([{
                # Puerto ficticio, y se nota en el rotulo. Existe para que un
                # consumidor que solo recorra el arbol NO se pierda un tanque.
                "rotulo": "sin ubicar",
                "estado": "desconocido",
                "entregando": None,
                "ma": None,
                "w": None,
                "hijos": huerfanos,
            }] if huerfanos else []),
        },
    }

    # Alias que no corresponden a ningun equipo del arbol. Sin esto, un id mal
    # escrito en el config.yaml no hace NADA y no se entera nadie: el equipo
    # sigue saliendo con su clave cruda y parece que el alias "no funciona".
    ids = set()

    def _ids(n):
        ids.add(n.get("id"))
        for p in n.get("puertos", []):
            for h in p.get("hijos", []):
                _ids(h)

    _ids(doc["raiz"])
    doc["resumen"]["alias_sin_usar"] = sorted(k for k in ALIAS if k not in ids)
    return doc


def ingest_pse(cfg: dict):
    """Hilo propio para el ethertype del MPS. No toca la ingesta de tanques."""
    iface = find_iface(cfg["ingest"]["iface_driver"], cfg["ingest"].get("iface", ""))
    print(f"[pse] escuchando ethertype 0x{MPS_ETHERTYPE:04X} en {iface}")
    try:
        s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW,
                          socket.htons(MPS_ETHERTYPE))
        s.bind((iface, 0))
        s.settimeout(1.0)
    except OSError as e:
        print(f"[pse] no pude abrir el socket: {e}", file=sys.stderr)
        return

    while not STOP.is_set():
        try:
            pkt = s.recv(512)
        except socket.timeout:
            continue
        except OSError as e:
            print(f"[pse] {e}", file=sys.stderr)
            time.sleep(1)
            continue
        payload = pkt[14:]
        mac = ":".join(f"{b:02x}" for b in pkt[6:12])

        fr = parse_mps_frame(payload)
        if fr is not None:
            # PSE_LIVE se mantiene ADEMAS del registro: /api/pse es la vista
            # historica del unico power switch y hay clientes colgando de ella.
            # Duplicar aqui es barato y evita romperlos.
            with PSE_LOCK:
                PSE_LIVE["ts"] = now()
                PSE_LIVE["mac"] = mac
                PSE_LIVE["puertos"] = fr["puertos"]
                PSE_LIVE["uptime_s"] = fr["uptime_s"]
                PSE_LIVE["seq"] = fr["seq"]
                PSE_LIVE["chip"] = fr.get("chip")
            registrar_switch("mps", mac, fr)
            continue

        fr = parse_mfs_frame(payload)
        if fr is not None:
            registrar_switch("mfs", mac, fr)
            continue
    s.close()


def pse_snapshot() -> dict:
    """Copia de la ultima telemetria, con su antiguedad y la potencia por slot.

    P = Vin x I. La tension sale de `vin_mv`, el Vin que midio la ultima
    clasificacion del LTC4296.

    ⚠️ Ese valor solo se refresca cuando corre una clasificacion, y el MPS
    solo reintenta en los puertos que NO entregan: con los 4 slots entregando
    a la vez, la tension se congela en la ultima medida. Para vatios exactos
    con todo cargado habria que leer Vout por puerto con el GADC.

    ⚠️ Es la tension de ENTRADA, no la de salida. El error es ~0,1 % (a 60 mA
    la resistencia de sensado cae 16 mV sobre 53,8 V), asi que sirve de sobra
    para dimensionar, pero es una estimacion.
    """
    with PSE_LOCK:
        d = dict(PSE_LIVE)
        d["puertos"] = [dict(p) for p in PSE_LIVE["puertos"]]
    d["edad_s"] = (now() - d["ts"]) if d["ts"] else None
    # Sin trama reciente el panel no debe dar por buenos los ultimos mA.
    d["vivo"] = d["edad_s"] is not None and d["edad_s"] <= 10

    # Potencia por slot. SIN DATO NO ES CERO: si falta la corriente o la
    # tension queda None y el panel pinta un guion. Un 0,0 W en un puerto que
    # entrega se leeria como "conectado y sin consumo".
    vin_mv = (d.get("chip") or {}).get("vin_mv")
    total = None
    for p in d["puertos"]:
        ma = p.get("ma")
        if vin_mv is None or ma is None:
            p["w"] = None
        else:
            p["w"] = round(vin_mv * ma / 1e6, 3)
            total = (total or 0.0) + p["w"]
    d["w_total"] = None if total is None else round(total, 3)
    d["w_vin_mv"] = vin_mv          # la tension usada, para poder auditarlo
    return d
# Centinela de "sin referencia" para el nivel (32 bits). La ATT lo manda
# cuando su cuenta arranco sin checkpoint: la calibracion no es aplicable.
ATT_LEVEL_NA = -2147483648


def parse_att_frame(payload: bytes):
    """Decodifica el payload (sin cabecera Ethernet).

    Devuelve un dict, o el entero de la version si no la conocemos, o None
    si no es una trama nuestra.
    """
    if len(payload) < 6:
        return None
    magic, ver = struct.unpack("!IH", payload[:6])
    if magic != MAGIC:
        return None
    fmt = FRAME_FMT_BY_VER.get(ver)
    if fmt is None:
        return ver
    n = FRAME_LEN_BY_VER[ver]
    if len(payload) < n:
        return None
    f = struct.unpack(fmt, payload[:n])
    level = temp = humi = pwr = None
    if ver == 1:
        _, _, tank, seq, count, edges, errors, uptime = f
    elif ver == 2:
        _, _, tank, seq, count, level, edges, errors, uptime = f
    else:
        if ver == 3:
            (_, _, tank, seq, count, level, edges, errors, uptime,
             temp, humi) = f
        else:
            (_, _, tank, seq, count, level, edges, errors, uptime,
             temp, humi, pwr) = f
        # La ATT no lleva sensor de humedad: manda el centinela. Guardar
        # None y no 0.0, para no inventar una lectura que no existe.
        temp = None if temp == ATT_NA else temp / 10.0
        humi = None if humi == ATT_NA else humi / 10.0
    # Sin referencia la CUENTA no significa nada, asi que NINGUNA calibracion
    # es aplicable -- ni la de la ATT ni la de la TPU. Se marca explicitamente
    # en vez de deducirlo de level_mm: en v1 ese campo no existe y vale None
    # sin que eso signifique nada malo.
    ref_ok = True
    if level == ATT_LEVEL_NA:
        level = None
        ref_ok = False
    d = {"ver": ver, "tank_id": tank, "seq": seq, "count": count,
         "level_mm": level, "ref_ok": ref_ok, "edges": edges,
         "errors": errors, "uptime": uptime, "temp_c": temp,
         "humi_rh": humi}
    if pwr is not None:
        # v4: estado de alimentacion, desglosado aqui para que nadie tenga que
        # recordar que significa cada bit -- y sobre todo para poder VERIFICAR
        # LA POLARIDAD DE PB11 de cada placa SIN ir con la consola: en tank1
        # vale 0 con alimentacion externa y en tank21 vale 1 en la MISMA
        # situacion, y activar la suspension por bateria en una placa invertida
        # la deja MUDA e indistinguible de un sensor averiado.
        #
        # ⚠️ `pb11` solo significa algo si `gpio_listo` es True.
        d["pwr"] = {
            "pb11": bool(pwr & 0x01),        # nivel CRUDO del pin
            "en_bateria": bool(pwr & 0x02),  # lo que concluye el firmware
            "bat_activa": bool(pwr & 0x04),  # CFG_F_BAT encendida en esa placa
            "gpio_listo": bool(pwr & 0x08),
            "crudo": pwr,
        }
    return d

STOP = threading.Event()


# ================================================================ utilidades
def now() -> int:
    return int(time.time())


def find_iface(driver: str, forced: str = "") -> str:
    """Localiza la interfaz SPE por DRIVER, nunca por nombre: los nombres
    eth1/eth2/eth3 se intercambian entre arranques en esta CM5."""
    if forced:
        return forced
    base = "/sys/class/net"
    for name in sorted(os.listdir(base)):
        try:
            drv = os.path.basename(os.readlink(f"{base}/{name}/device/driver"))
            if drv == driver:
                return name
        except OSError:
            continue
    raise RuntimeError(f"no encuentro ninguna interfaz con driver '{driver}'")


# ================================================================ almacen
class Store:
    """SQLite en el SSD. Un solo hilo escribe (cola) para no pelear por el
    lock; los lectores usan sus propias conexiones en modo WAL."""

    def __init__(self, path: str, retention: dict):
        self.path = path
        self.retention = retention
        self.q: "queue.Queue" = queue.Queue(maxsize=10000)
        # ★ EL HISTORIADOR NO PUEDE TUMBAR LA MEDIDA (2026-08-06).
        #
        # Antes, si la base no se podia abrir, Store() lanzaba y el proceso
        # entero moria: con el SSD caido se perdian TAMBIEN el MQTT, el Modbus,
        # el panel y la ingesta. Ocurrio de verdad -- el NVMe de esta CM5 se cae
        # a los ~20 min de cada arranque (D3cold del que no vuelve, con el
        # ahorro ya desactivado en cmdline) y dejo la planta sin pasarela.
        #
        # Ahora arranca igualmente en modo DEGRADADO: se sirve el dato en vivo,
        # se cuenta lo que se pierde y se reintenta abrir la base sola. Un
        # historiador caido es un problema; que ademas apague la instrumentacion
        # es inaceptable.
        self.disponible = False
        self.perdidas = 0            # muestras tiradas por no haber base
        self.ultimo_error = None
        self._probar()
        threading.Thread(target=self._writer, daemon=True, name="store").start()
        threading.Thread(target=self._roller, daemon=True, name="roll").start()
        threading.Thread(target=self._reintentar, daemon=True, name="store-re").start()

    def _probar(self) -> bool:
        """Intenta dejar la base utilizable. Devuelve si lo consiguio."""
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            self._init_schema()
        except Exception as e:
            if self.disponible or self.ultimo_error is None:
                print(f"[store] ⛔ SIN ALMACENAMIENTO: {e}. La pasarela sigue "
                      f"sirviendo el dato en VIVO, pero NO se guarda historico.",
                      file=sys.stderr)
            self.disponible = False
            self.ultimo_error = str(e)
            return False
        if not self.disponible:
            print(f"[store] almacenamiento disponible en {self.path}"
                  + (f" (se perdieron {self.perdidas} muestras)"
                     if self.perdidas else ""))
        self.disponible = True
        self.ultimo_error = None
        return True

    def _reintentar(self):
        """Vigila el almacenamiento y lo recupera solo.

        DOS trabajos, y el segundo faltaba:

          - si esta CAIDO, reintenta abrirlo (cada 30 s). Si el disco vuelve,
            el historico se reanuda sin reiniciar el servicio.

          - si esta DISPONIBLE, comprueba cada 5 s que de verdad lo esta.

        ⚠️ Sin esa comprobacion, `disponible` solo se ponia a False desde
        put(), o sea desde el camino de ESCRITURA. Las lecturas (roster,
        tank_cfg, el arbol) miran la bandera al entrar pero no capturan la
        excepcion: si el disco moria con la bandera aun en True, reventaban y
        se llevaban por delante TODA la API -- panel en blanco, /api/tanks y
        /api/arbol devolviendo nada. El modo degradado existia justo para que
        eso no pasara y no llegaba a activarse.

        Paso el 2026-08-07: ext4 cerro el sistema de ficheros por errores de
        E/S del NVMe (`mount` lo mostraba como `shutdown`) y la pasarela seguia
        recibiendo telemetria con normalidad mientras el panel estaba muerto.
        """
        n = 0
        while not STOP.is_set():
            for _ in range(5):
                if STOP.is_set():
                    return
                time.sleep(1)
            n += 5
            if self.disponible:
                # Sondeo barato: si el volumen esta caido, esto falla enseguida.
                try:
                    c = self._conn()
                    c.execute("SELECT 1 FROM tanks LIMIT 1").fetchone()
                    c.close()
                except Exception as e:
                    self.disponible = False
                    self.ultimo_error = str(e)
                    print(f"[store] ⛔ EL ALMACENAMIENTO HA CAIDO: {e}. Se sigue "
                          f"sirviendo el dato en VIVO, pero NO se guarda "
                          f"historico.", file=sys.stderr)
            elif n % 30 == 0:
                self._probar()

    def estado(self) -> dict:
        """Salud del almacenamiento, para publicarla. Un fallo de disco fue
        SILENCIOSO la primera vez: los niveles seguian llegando y el panel se
        veia normal mientras no se guardaba nada."""
        return {"disponible": self.disponible, "ruta": self.path,
                "muestras_perdidas": self.perdidas, "error": self.ultimo_error}

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        return c

    def _init_schema(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "schema.sql")) as f:
            sql = f.read()
        c = self._conn()
        c.executescript(sql)
        # La base ya desplegada no tiene las columnas de ambiente: anadirlas
        # aqui. ALTER TABLE ... ADD COLUMN es barato y no toca los datos.
        for col, typ in (("temp_c", "REAL"), ("humi_rh", "REAL")):
            try:
                c.execute(f"ALTER TABLE samples_raw ADD COLUMN {col} {typ}")
            except Exception:
                pass   # ya existia
        c.commit()
        c.close()

    def put(self, rec: dict):
        if not self.disponible:
            self.perdidas += 1       # se cuenta, no se oculta
            return
        try:
            self.q.put_nowait(rec)
        except queue.Full:
            # Perder una muestra es preferible a bloquear la ingesta: el
            # siguiente push llega en 30s. Se registra para no ocultarlo.
            print("[store] cola llena, muestra descartada", file=sys.stderr)

    def _writer(self):
        # ⚠️ Conexion PEREZOSA: antes se abria aqui y, si la base no estaba, el
        # hilo moria en silencio para siempre -- ni siquiera al volver el disco
        # se reanudaba. Ahora se abre cuando hace falta y se suelta si falla.
        c = None
        pend = []
        last_flush = time.time()
        while not STOP.is_set():
            if not self.disponible:
                if c is not None:
                    try:
                        c.close()
                    except Exception:
                        pass
                    c = None
                pend.clear()
                time.sleep(1)
                continue
            if c is None:
                try:
                    c = self._conn()
                except Exception as e:
                    self.disponible = False
                    self.ultimo_error = str(e)
                    continue
            try:
                pend.append(self.q.get(timeout=1.0))
            except queue.Empty:
                pass
            # Agrupar escrituras: con 30+ tanques evita un commit por trama
            # y reduce el desgaste del SSD.
            if pend and (len(pend) >= 50 or time.time() - last_flush > 5):
                try:
                    c.executemany(
                        "INSERT OR REPLACE INTO samples_raw"
                        "(ts,tank_id,count,value,edges,errors,uptime_s,"
                        "temp_c,humi_rh)"
                        " VALUES(?,?,?,?,?,?,?,?,?)",
                        [(r["ts"], r["tank_id"], r["count"], r["value"],
                          r["edges"], r["errors"], r["uptime_s"],
                          r.get("temp_c"), r.get("humi_rh")) for r in pend],
                    )
                    c.commit()
                except Exception as e:
                    print(f"[store] error escribiendo: {e}", file=sys.stderr)
                    # Si la base se cae en caliente hay que enterarse aqui:
                    # seguir encolando llenaria la cola y taparia el problema.
                    self.disponible = False
                    self.ultimo_error = str(e)
                    try:
                        c.close()
                    except Exception:
                        pass
                    c = None
                pend.clear()
                last_flush = time.time()
        # `c` puede ser None: la conexion es perezosa desde que el almacen
        # puede estar caido. Cerrar a ciegas mataba el hilo al parar.
        if c is not None:
            try:
                c.close()
            except Exception:
                pass

    def _roller(self):
        """Consolida crudo -> 1m -> 1h y purga lo vencido. Sin esto, el
        historico largo no cabe."""
        while not STOP.is_set():
            for _ in range(600):                 # cada 10 min, saliendo rapido
                if STOP.is_set():
                    return
                time.sleep(1)
            if not self.disponible:
                continue
            try:
                c = self._conn()
                # crudo -> minuto
                c.execute("""
                    INSERT OR REPLACE INTO samples_1m
                      (ts,tank_id,value_avg,value_min,value_max,n,errors)
                    SELECT (ts/60)*60, tank_id, AVG(value), MIN(value), MAX(value),
                           COUNT(*), MAX(errors)
                      FROM samples_raw WHERE ts < strftime('%s','now') - 120
                     GROUP BY tank_id, ts/60
                """)
                # minuto -> hora
                c.execute("""
                    INSERT OR REPLACE INTO samples_1h
                      (ts,tank_id,value_avg,value_min,value_max,n,errors)
                    SELECT (ts/3600)*3600, tank_id, AVG(value_avg), MIN(value_min),
                           MAX(value_max), SUM(n), MAX(errors)
                      FROM samples_1m WHERE ts < strftime('%s','now') - 7200
                     GROUP BY tank_id, ts/3600
                """)
                # retencion
                r = self.retention
                c.execute("DELETE FROM samples_raw WHERE ts < ?",
                          (now() - r["raw_days"] * 86400,))
                c.execute("DELETE FROM samples_1m WHERE ts < ?",
                          (now() - r["minute_days"] * 86400,))
                c.execute("DELETE FROM samples_1h WHERE ts < ?",
                          (now() - r["hour_days"] * 86400,))
                c.commit()
                c.close()
            except Exception as e:
                print(f"[store] error consolidando: {e}", file=sys.stderr)

    def upsert_tank(self, tank_id: int, mac: str):
        if not self.disponible:
            return                   # sin base no hay nada que registrar
        c = self._conn()
        row = c.execute("SELECT mac FROM tanks WHERE tank_id=?", (tank_id,)).fetchone()
        if row is None:
            c.execute("INSERT INTO tanks(tank_id,mac,first_seen,last_seen) VALUES(?,?,?,?)",
                      (tank_id, mac, now(), now()))
            c.execute("INSERT INTO events(ts,tank_id,kind,detail) VALUES(?,?,?,?)",
                      (now(), tank_id, "online", f"alta automatica, mac={mac}"))
        else:
            if row[0] and row[0] != mac:
                # Dos placas con el mismo tank_id: error de campo. Gritarlo.
                c.execute("INSERT INTO id_conflicts(ts,tank_id,mac_old,mac_new)"
                          " VALUES(?,?,?,?)", (now(), tank_id, row[0], mac))
                c.execute("INSERT INTO events(ts,tank_id,kind,detail) VALUES(?,?,?,?)",
                          (now(), tank_id, "conflict",
                           f"tank_id duplicado: {row[0]} -> {mac}"))
                print(f"[!] CONFLICTO: tank_id {tank_id} visto en {row[0]} y {mac}",
                      file=sys.stderr)
            c.execute("UPDATE tanks SET mac=?, last_seen=? WHERE tank_id=?",
                      (mac, now(), tank_id))
        c.commit()
        c.close()

    def set_cal(self, tank_id: int, scale: float, offset: float,
                unit: str = None):
        """Guarda la recta de calibracion de un tanque.

        Vive en la TPU, no en el sensor: la ATT manda la cuenta cruda y el
        escalado se aplica aqui. Asi, sustituir una placa averiada no
        obliga a recalibrar -- basta con ponerle su tank_id.
        """
        if not self.disponible:
            return               # sin base no hay nada que guardar
        c = self._conn()
        c.execute("INSERT OR IGNORE INTO tanks(tank_id,first_seen,last_seen)"
                  " VALUES(?,?,?)", (tank_id, now(), now()))
        if unit:
            c.execute("UPDATE tanks SET scale=?,offset=?,unit=? WHERE tank_id=?",
                      (scale, offset, unit, tank_id))
        else:
            c.execute("UPDATE tanks SET scale=?,offset=? WHERE tank_id=?",
                      (scale, offset, tank_id))
        c.commit()
        c.close()

    def roster(self, live: dict) -> list:
        """Todos los tanques CONOCIDOS, no solo los que reportan ahora.

        Un sensor que deja de responder es informacion operativa y tiene
        que seguir en la lista, marcado como caido. Si solo se listara lo
        vivo, un reinicio de la pasarela borraria de la vista justo los
        sensores que hay que ir a revisar.
        """
        if not self.disponible:
            # ⚠️ Sin base se sirve SOLO lo vivo. Es peor que el registro
            # completo -- los tanques caidos desde el arranque no salen -- pero
            # es mucho mejor que devolver un error y dejar el panel en blanco.
            return [dict(r, age_s=now() - r["ts"],
                         online=(now() - r["ts"]) <= 120)
                    for r in (live or {}).values()]
        c = self._conn()
        out, seen = [], set()
        t_now = now()
        for tid, name, mac, unit, last_seen, scale, offset in c.execute(
                "SELECT tank_id,name,mac,unit,last_seen,scale,offset FROM tanks"
                " ORDER BY tank_id"):
            seen.add(tid)
            # Periodo OBSERVADO entre las dos ultimas muestras. Es la prueba
            # de que un cambio de periodo llego de verdad al sensor.
            # OJO: samples_raw tiene PK (tank_id, ts) con ts en SEGUNDOS, asi
            # que por debajo de 1 s las tramas se solapan en la misma fila y
            # esto no puede bajar de 1. Para el rango util (1 s - 1 min) vale.
            per = c.execute("SELECT ts FROM samples_raw WHERE tank_id=?"
                            " ORDER BY ts DESC LIMIT 2", (tid,)).fetchall()
            period = (per[0][0] - per[1][0]) if len(per) == 2 else None
            if tid in live:
                r = dict(live[tid])
                r["period_s"] = period
                r["scale"], r["offset"] = scale, offset
                out.append(r)
                continue
            # No reporta: reconstruir su ultima medida conocida del historico
            row = c.execute(
                "SELECT ts,count,value,errors,temp_c,humi_rh FROM samples_raw"
                " WHERE tank_id=? ORDER BY ts DESC LIMIT 1", (tid,)).fetchone()
            ts = (row[0] if row else last_seen) or 0
            out.append({
                "tank_id": tid, "name": name or f"tank{tid}", "mac": mac,
                "unit": unit or "mm",
                "count": row[1] if row else None,
                "value": row[2] if row else None,
                "errors": row[3] if row else None,
                "temp_c": row[4] if row else None,
                "humi_rh": row[5] if row else None,
                "ts": ts, "age_s": (t_now - ts) if ts else None,
                "period_s": period, "online": False,
                "scale": scale, "offset": offset,
            })
        # Vivos que aun no estan en la tabla (alta en curso)
        for tid, r in live.items():
            if tid not in seen:
                out.append(r)
        c.close()
        out.sort(key=lambda t: t["tank_id"])
        return out

    def tank_cfg(self) -> dict:
        if not self.disponible:
            return {}        # sin base: no se inventa, se devuelve vacio
        c = self._conn()
        out = {r[0]: {"name": r[1], "scale": r[2], "offset": r[3], "unit": r[4]}
               for r in c.execute("SELECT tank_id,name,scale,offset,unit FROM tanks")}
        c.close()
        return out

    def history(self, tank_id: int, table: str, since: int, limit: int = 5000):
        if not self.disponible:
            return []        # sin base: no se inventa, se devuelve vacio
        c = self._conn()
        rows = c.execute(
            f"SELECT * FROM {table} WHERE tank_id=? AND ts>=? ORDER BY ts DESC LIMIT ?",
            (tank_id, since, limit)).fetchall()
        c.close()
        return rows


# ================================================================ estado vivo
class Live:
    """Ultimo valor de cada tanque, en RAM. Es lo que consultan MQTT, Modbus
    y HTTP: nadie toca el disco para leer el valor actual."""

    def __init__(self, offline_after: int):
        self.lock = threading.Lock()
        self.tanks: dict = {}
        # Placas que reportan tank_id=0, indexadas por MAC. VAN APARTE de
        # `tanks` a proposito: no son tanques todavia, no tienen historico y no
        # deben contarse en los resumenes. Pero tienen que VERSE, o dar de alta
        # una placa exige adivinar su MAC leyendo el log del DHCP -- que es
        # exactamente lo que hubo que hacer a mano el 2026-08-07.
        self.nuevas: dict = {}
        self.offline_after = offline_after

    def update(self, rec: dict):
        with self.lock:
            self.tanks[rec["tank_id"]] = rec

    def update_nueva(self, rec: dict):
        with self.lock:
            self.nuevas[rec["mac"]] = rec

    def olvidar_nueva(self, mac: str):
        """Se llama al asignarle identidad: deja de ser una placa nueva."""
        with self.lock:
            self.nuevas.pop(mac, None)

    def snapshot(self) -> dict:
        t = now()
        with self.lock:
            out = {}
            for tid, r in self.tanks.items():
                d = dict(r)
                d["age_s"] = t - r["ts"]
                d["online"] = d["age_s"] <= self.offline_after
                out[tid] = d
            return out

    def nuevas_snapshot(self) -> list:
        """Placas sin asignar vistas hace poco.

        Se OLVIDAN las que llevan un rato sin hablar (10x el plazo normal):
        una placa que se desconecto no debe quedarse en la lista invitando a
        asignarle un tanque que ya no esta ahi.
        """
        t = now()
        with self.lock:
            out = []
            for mac, r in self.nuevas.items():
                edad = t - r["ts"]
                if edad > self.offline_after * 10:
                    continue
                d = dict(r)
                d["age_s"] = edad
                d["online"] = edad <= self.offline_after
                out.append(d)
            return sorted(out, key=lambda x: x["mac"])


# ================================================================ ingesta
_warned_ver: set = set()
_warned_id: set = set()


def ingest(cfg: dict, store: Store, live: Live, outs: list):
    iface = find_iface(cfg["ingest"]["iface_driver"], cfg["ingest"].get("iface", ""))
    print(f"[spe] escuchando ethertype 0x{ETHERTYPE:04X} en {iface}")
    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETHERTYPE))
    s.bind((iface, 0))
    s.settimeout(1.0)
    tcfg = store.tank_cfg()
    last_cfg = time.time()

    while not STOP.is_set():
        try:
            pkt = s.recv(2048)
        except socket.timeout:
            continue
        except OSError as e:
            print(f"[spe] {e}", file=sys.stderr)
            time.sleep(1)
            continue

        if len(pkt) < 14 + FRAME_MIN_LEN:
            continue
        mac = ":".join(f"{b:02x}" for b in pkt[6:12])
        fr = parse_att_frame(pkt[14:])
        if fr is None:
            continue
        if isinstance(fr, int):
            ver = fr
            # Una ATT con firmware distinto: mejor decirlo que interpretar mal
            # sus bytes. Se avisa una vez por MAC, no en cada trama.
            if mac not in _warned_ver:
                _warned_ver.add(mac)
                print(f"[spe] {mac} habla version {ver}, no soportada"
                      f" -- ignorada", file=sys.stderr)
            continue
        ver, tank_id, seq = fr["ver"], fr["tank_id"], fr["seq"]
        count, edges, errors = fr["count"], fr["edges"], fr["errors"]
        uptime = fr["uptime"]
        if tank_id == 0:
            # 0 = sin asignar: una placa recien puesta en marcha. NO es un
            # tanque -- no va al historico ni a los resumenes -- pero si se
            # PUBLICA como placa pendiente, para poder darla de alta desde el
            # panel sin ir a buscar su MAC al log del DHCP.
            #
            # Antes se descartaba la trama y se avisaba por stderr. El aviso
            # era correcto y no lo leyo nadie: bajo systemd la salida va con
            # buffer y no aparece hasta que el servicio para. Una placa nueva
            # quedaba invisible aunque estuviera transmitiendo bien.
            live.update_nueva({
                "ts": now(), "mac": mac, "seq": seq, "count": count,
                "edges": edges, "errors": errors, "uptime_s": uptime // 1000,
                "temp_c": fr["temp_c"], "humi_rh": fr["humi_rh"],
                "ref_ok": fr["ref_ok"], "ver": ver,
                **({"pwr": fr["pwr"]} if fr.get("pwr") is not None else {}),
            })
            if mac not in _warned_id:
                _warned_id.add(mac)
                print(f"[spe] {mac} reporta tank_id=0 (sin asignar): aparece en"
                      f" el panel para darle numero de tanque", file=sys.stderr)
            continue

        # Ya tiene identidad: deja de ser una placa pendiente. Se hace AQUI, al
        # ver la primera trama con tank_id valido, y no solo al darla de alta
        # desde el panel: la identidad se le puede escribir por Modbus desde
        # cualquier sitio (una consola, un script de puesta en marcha), y
        # entonces la placa seguia figurando como "sin asignar" hasta que
        # caducaba. El 2026-08-07 la tank7 salio a la vez en las dos tablas.
        if mac in live.nuevas:
            live.olvidar_nueva(mac)

        if time.time() - last_cfg > 30:
            tcfg = store.tank_cfg()
            last_cfg = time.time()
        if tank_id not in tcfg:
            store.upsert_tank(tank_id, mac)
            tcfg = store.tank_cfg()
        else:
            store.upsert_tank(tank_id, mac)

        c = tcfg.get(tank_id, {})
        rec = {
            "ts": now(), "tank_id": tank_id, "mac": mac, "seq": seq,
            "count": count, "edges": edges, "errors": errors,
            # La ATT manda MILISEGUNDOS (k_uptime_get_32). Convertir aqui: un
            # uptime de "121742 s" tras un reinicio delata el error de unidades.
            "uptime_s": uptime // 1000,
            # None = la ATT no tiene referencia: la cuenta no significa nada
            # y aplicarle la recta daria un valor falso pero plausible.
            #
            # ⚠️ SIN CALIBRAR **SI** SE PUBLICA, y sale el CONTEO del encoder:
            # la recta es la identidad y lo deja pasar tal cual, con `unit`
            # diciendo "mm". DECISION DELIBERADA (2026-08-11), no un descuido:
            # callarlo dejaria el tanque indistinguible de un sensor caido, y
            # durante la puesta en marcha de 50 tanques eso son decenas de
            # huecos que nadie sabria interpretar.
            #
            # A cambio, el consumidor tiene con que distinguirlo SIEMPRE:
            # `calibrado` va en la trama <id>/raw y en el nodo del arbol, donde
            # ademas todos los numeros de calibracion van a null.
            "value": (count * (c.get("scale") or 1.0) + (c.get("offset") or 0.0)
                      if fr["ref_ok"] else None),
            "ref_ok": fr["ref_ok"],
            # ★ Va en el estado VIVO para que las salidas no tengan que
            # re-deducirlo cada una por su cuenta -- y sobre todo para que
            # Modbus pueda decirlo, que no tiene forma de mandar un null.
            # Recta identidad = sin calibrar: el "nivel" son PULSOS.
            "calibrado": not ((c.get("scale") or 1.0) == 1.0
                              and (c.get("offset") or 0.0) == 0.0),
            "unit": c.get("unit") or "mm",
            "name": c.get("name") or f"tank{tank_id}",
            # Ambiente (v3). None = la placa no lo reporta.
            "temp_c": fr["temp_c"],
            "humi_rh": fr["humi_rh"],
            # Nivel que calcula la propia ATT (v2+). La calibracion BUENA es
            # la de la TPU (scale/offset); esto queda como referencia.
            "att_level_mm": fr["level_mm"],
        }
        # v4: estado de alimentacion de la placa (PB11). Solo si la trama lo
        # trae; con firmware anterior no existe y no se inventa.
        if fr.get("pwr") is not None:
            rec["pwr"] = fr["pwr"]
        live.update(rec)
        store.put(rec)
        for o in outs:
            try:
                o.on_sample(rec)
            except Exception as e:
                print(f"[out] {e}", file=sys.stderr)
    s.close()


# ============================================ ingesta por Modbus RTU
# El SPE del ATT (ADIN2111/OA-SPI) esta roto, asi que el ATT solo habla por
# RS-485. Aqui la gateway hace de MAESTRO Modbus: consulta al ATT (esclavo) y
# mete el mismo `rec` en el pipeline (Store/Live/outputs), igual que la ingesta
# SPE. Usa el nivel ya calibrado por el ATT (IR7-8) como valor.
def _rtu_crc16(d: bytes) -> int:
    c = 0xFFFF
    for b in d:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
    return c


def _rtu_read(ser, unit: int, func: int, addr: int, count: int):
    req = struct.pack(">BBHH", unit, func, addr, count)
    req += struct.pack("<H", _rtu_crc16(req))
    ser.reset_input_buffer()
    ser.write(req)
    hdr = ser.read(3)                       # unit, func, byte_count
    if len(hdr) < 3 or hdr[0] != unit or hdr[1] != func:
        return None
    nbytes = hdr[2]
    body = ser.read(nbytes + 2)             # datos + CRC
    if len(body) < nbytes + 2:
        return None
    data = body[:nbytes]
    if struct.unpack("<H", body[nbytes:nbytes + 2])[0] != _rtu_crc16(hdr + data):
        return None
    return struct.unpack(">" + "H" * (nbytes // 2), data)


def _s32(hi: int, lo: int) -> int:
    v = ((hi & 0xFFFF) << 16) | (lo & 0xFFFF)
    return v - (1 << 32) if v & 0x80000000 else v


def ingest_modbus(cfg: dict, store: Store, live: Live, outs: list):
    mc = cfg.get("modbus_ingest", {})
    import serial
    par = {"N": serial.PARITY_NONE, "E": serial.PARITY_EVEN,
           "O": serial.PARITY_ODD}[mc.get("parity", "E")]
    dev = mc.get("device", "/dev/ttyAMA2")
    unit = mc.get("unit_id", 1)
    poll = mc.get("poll_s", 2)
    fallback_id = mc.get("tank_id", 0)      # si el ATT reporta tank_id=0
    try:
        ser = serial.Serial(dev, mc.get("baud", 19200), parity=par,
                            stopbits=1, bytesize=8, timeout=0.4)
    except Exception as e:
        print(f"[rtu] no pude abrir {dev}: {e}", file=sys.stderr)
        return
    print(f"[rtu] ingesta Modbus del ATT en {dev} unit {unit} cada {poll}s")
    while not STOP.is_set():
        ir = _rtu_read(ser, unit, 4, 0, 10)     # IR 0-9
        hr = _rtu_read(ser, unit, 3, 5, 1)      # HR 5 = tank_id
        if ir is None:
            time.sleep(poll)
            continue
        count    = _s32(ir[0], ir[1])
        edges    = ((ir[2] & 0xFFFF) << 16) | (ir[3] & 0xFFFF)
        errors   = ir[4]
        uptime_s = ir[5]                        # IR5 ya viene en segundos
        level    = _s32(ir[7], ir[8])           # nivel mm (calibrado por el ATT)
        cal      = ir[9]
        tank_id  = (hr[0] if hr else 0) or fallback_id
        if tank_id == 0:
            time.sleep(poll)
            continue                            # sin asignar: no es un dato

        store.upsert_tank(tank_id, dev)
        c = store.tank_cfg().get(tank_id, {})
        # Los registros 7/8 salen del mismo att_level_mm(), asi que traen el
        # centinela igual que la trama SPE.
        ref_ok = (level != ATT_LEVEL_NA)
        base = level if cal else count          # calibrado -> nivel; si no, cuenta
        rec = {
            "ts": now(), "tank_id": tank_id, "mac": dev, "seq": 0,
            "count": count, "edges": edges, "errors": errors,
            "uptime_s": uptime_s,
            "value": (base * (c.get("scale") or 1.0) + (c.get("offset") or 0.0)
                      if ref_ok else None),
            "ref_ok": ref_ok,
            "unit": c.get("unit") or "mm",
            "name": c.get("name") or f"tank{tank_id}",
        }
        live.update(rec)
        store.put(rec)
        for o in outs:
            try:
                o.on_sample(rec)
            except Exception as e:
                print(f"[out] {e}", file=sys.stderr)
        time.sleep(poll)
    ser.close()


# ================================================================ main
def load_cfg(path: str) -> dict:
    try:
        import yaml
    except ImportError:
        print("Falta PyYAML:  sudo apt install python3-yaml", file=sys.stderr)
        sys.exit(1)
    with open(path) as f:
        return yaml.safe_load(f)


def main():
    ap = argparse.ArgumentParser(description="Pasarela Varec (SPE -> MQTT/Modbus/HTTP)")
    ap.add_argument("--config", default="/etc/varec-gateway/config.yaml")
    args = ap.parse_args()
    cfg = load_cfg(args.config)
    cfg["_path"] = args.config          # la UI necesita saber donde guardar

    def stop(*_):
        print("\n[gw] parando...")
        STOP.set()
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    store = Store(cfg["store"]["path"], cfg["store"]["retention"])
    live = Live(cfg["ingest"]["offline_after_s"])

    outs = []
    from outputs import build_outputs           # noqa: E402
    # Alias legibles. Se cargan aqui y no en cada foto: cambiarlos exige
    # reiniciar el servicio, que es lo razonable para algo que se toca una vez.
    ALIAS.update({str(k): str(v) for k, v in (cfg.get("alias") or {}).items()})
    if ALIAS:
        print(f"[alias] {len(ALIAS)} nombre(s) legible(s) cargado(s)")

    def arbol_fn():
        """Arbol completo de la instalacion, ya resuelto. Se pasa como funcion
        y no como dato porque quien lo publica (MQTT) corre en su propio hilo y
        necesita una foto FRESCA en cada envio, no la de cuando arranco.

        ⚠️ Los tanques salen de store.roster(), NO de live.snapshot(): el vivo
        solo tiene lo visto desde que arranco el proceso, asi que un reinicio
        de la pasarela borraria del arbol justo los sensores caidos, que son
        los que hay que ir a revisar. Es la misma razon por la que /api/tanks
        usa el registro persistente.
        """
        snap = live.snapshot()
        macs = {r["mac"]: r["tank_id"] for r in snap.values() if r.get("mac")}
        tanques = {r["tank_id"]: r for r in store.roster(snap)}
        return arbol(switches_snapshot(macs), tanques, store.estado(),
                     live.nuevas_snapshot())

    def cal_fn(orden):
        """Calibracion pedida desde la sala de visualizacion por MQTT."""
        return aplicar_calibracion(store, live, orden)

    outs = build_outputs(cfg, live, store, STOP, pse_fn=pse_snapshot,
                         switches_fn=switches_snapshot, arbol_fn=arbol_fn,
                         cal_fn=cal_fn)

    # Ingesta por dos caminos: SPE (tramas L2) y/o Modbus RTU (RS-485).
    # Con el SPE del ATT roto, el RTU es el que trae los datos.
    if cfg.get("modbus_ingest", {}).get("enabled"):
        threading.Thread(target=ingest_modbus, args=(cfg, store, live, outs),
                         daemon=True, name="rtu").start()
    if cfg["ingest"].get("enabled", True):
        threading.Thread(target=ingest, args=(cfg, store, live, outs),
                         daemon=True, name="spe").start()
        # Hilo aparte para la telemetria del MPS: si falla, la ingesta de
        # tanques -- que es la critica -- no se entera.
        threading.Thread(target=ingest_pse, args=(cfg,),
                         daemon=True, name="pse").start()

    while not STOP.is_set():
        time.sleep(0.5)
    print("[gw] fin")


if __name__ == "__main__":
    main()
