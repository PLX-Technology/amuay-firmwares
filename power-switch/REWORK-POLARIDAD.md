# Rework: par SPE invertido en los zócalos del MPS-04P

**Para Mayker.** Fecha del diagnóstico: 2026-07-30.

**Resumen en una línea:** el par SPE llega invertido al zócalo del slot en el
MPS-04P, el módulo PSM lo bloquea con sus diodos de protección de polaridad, y
por eso la negociación SPoE no arranca nunca. **No tiene arreglo por
software.**

---

## 1. La evidencia

Con el puerto retenido en clasificación durante 120 s y el multímetro en el
**zócalo del slot** (donde se enchufa el módulo PSM), midiendo **los mismos
puntos con las mismas puntas** en las dos placas:

| Placa | Slot | Multímetro | `sccpi` durante SEARCHING | Negocia |
|---|---|---|---|---|
| **MFS** (referencia sana) | Port 2 | **+5 V** | 0 → **1** | ✅ |
| **MPS-04P** | slot 2 | **−5 V** | 0 (no sube) | ❌ |

Misma magnitud, signo opuesto. La tensión de clasificación **sí llega** al
zócalo — no hay corte ni pista abierta — pero con el par cambiado.

### Cadena causal

1. El par llega invertido al zócalo.
2. El módulo PSM ve tensión inversa. Sus diodos de protección de polaridad
   (`D1`/`D2`, BAS516) **bloquean**.
3. Al bloquear, el módulo no presenta firma de detección válida y **nunca
   levanta la línea `SCCPI`** hacia el micro.
4. El firmware llama a `sccp_reset_pulse()`, que comprueba que la línea esté
   alta **antes de nada**, y aborta con `PD_LINE_NOT_HIGH`.
5. La negociación se descarta y el puerto nunca entrega potencia.

### Lo que esto explica

- **Los cuatro puertos del MPS fallan igual** ⇒ es un error común de trazado,
  no un defecto por puerto ni un componente dañado.
- **Ningún módulo PSM se ha dañado nunca.** Los diodos de protección hicieron
  exactamente su trabajo. Los módulos están buenos: los mismos negocian sin
  problema en el MFS.
- **El `Port 6` del MFS falla idéntico** (`sccpi` clavado en 0 durante
  SEARCHING). Es el puerto que se habilitó el 2026-07-29 y que nunca se
  validó en hardware. **Muy probablemente tiene la misma inversión.**

---

## 2. Alcance: qué hay que medir antes de tocar nada

Solo está medida la polaridad del **slot 2 del MPS**. Los slots **3 y 4 nunca
se han probado con módulo**, y del slot 1 sabemos que falla pero no se midió
el signo.

**Medir la polaridad de los 4 slots del MPS y del `Port 6` del MFS** antes de
decidir el alcance del rework. Si algún slot estuviera bien cableado, se puede
usar en producción de inmediato mientras se corrigen los demás.

### Cómo medirlo (la ventana real dura 4 ms, no se pilla a mano)

En operación normal el puerto entra en clasificación **4 ms cada ~5 s**:
imposible de medir con un multímetro. Hay un script que lo deja **retenido en
clasificación 120 s**:

```bash
openocd -s C:/MaximSDK/Tools/OpenOCD/scripts -f interface/cmsis-dap.cfg \
  -f target/max32690.cfg -f tools/ltc4296-swd/mps_hold_clasif.tcl
```

Durante la cuenta atrás, medir en el zócalo entre los dos pines del par.
**Sano = +5 V. Invertido = −5 V.**

Requisitos para que la medida valga:

- **Riel a 54 V**, no a 50 (ver §5).
- **La placa tiene que estar corriendo la aplicación.** El script aborta solo
  si detecta que está en el ROM, pero conviene saberlo: si el Pico está
  conectado al SWD durante el encendido, el ROM no salta a la aplicación.
  Secuencia: cinta SWD fuera → POR de 10 s → esperar unos segundos a que
  termine el arranque seguro → conectar la cinta.
- El script está fijado al **puerto 1 (slot 2)**. Para otro slot hay que
  regenerar los frames SPI con `tools/ltc4296-swd/gen_frames.py`.

---

## 3. Localizar el error en el esquemático

**El MFS es la referencia buena.** El camino más fiable no es deducirlo, sino
**comparar el MPS contra el MFS** en el conector del slot: la diferencia es el
error.

Topología del MPS según las notas del esquemático (`MPS_SCH.png`, bloque
POWER SOURCE PSE) — **confirmar contra el esquemático real**:

- `U12` = LTC4296, PSE SPoE de 5 canales.
- `HGATEx` maneja un MOSFET **de lado alto** (PSMN075-100MSEX) que conecta
  `PSE_IN` (54 V) a `Px_PWR_P`. Mapeo anotado: canal 0 → `Q17` → `P1_PWR_P`,
  canal 1 → `Q16` → `P2_PWR_P`, canal 2 → `Q15` → `P3_PWR_P`,
  canal 3 → `Q6` → `P4_PWR_P`.
- `Q18` (PSMN075) = switch de retorno de **lado bajo compartido**
  (`LGATE`/`LSRC`) ⇒ `PWR_N` es un nodo común a todos los puertos.
- Sensado: `R94`-`R97` (0.27 Ω, lado alto), `R103`-`R106` (0.068 Ω, lado bajo).

Es decir: **`Px_PWR_P` debe ir al pin positivo del zócalo y `PWR_N` al de
retorno.** Comprobar a qué pin del zócalo llega cada uno, y contrastarlo con
el pinout del módulo PSM y con cómo está en el MFS.

---

## 4. El arreglo

**Intercambiar las dos pistas del par de potencia en el zócalo de cada slot
afectado.**

### ⚠️ Hacerlo aguas abajo del sensado

El cruce va **entre los sensores y el zócalo**, cruzando simplemente las dos
pistas. Así el lazo de corriente sigue pasando por `R94`-`R97` (lado alto) y
`R103`-`R106` (lado bajo) exactamente igual que ahora, y el sensado no se
toca.

### ⛔ Lo que NO hay que hacer

- **No atar el retorno a masa** para "arreglar" la polaridad. `PWR_N` **no
  está a masa**: el LTC4296 sensa corriente por el **lado bajo**, y
  puentearlo a masa anula esa protección. Es la misma regla que prohíbe unir
  la masa de la ATT con la del field switch.
- **No cambiar el cable SPE.** La inversión está **antes** del módulo: si
  estuviera en el cable, el módulo habría visto la polaridad correcta y
  `SCCPI` habría subido. Cambiar los hilos del cable no arregla nada.
- **No esperar un arreglo por firmware.** El LTC4296 **no tiene ningún bit de
  polaridad**: se revisó el mapa completo de `PxCFG0`/`PxCFG1`. Lo único con
  "reverse" en el nombre es `LSNS_REVERSE_FAULT`, que es un bit de *estado*
  que reporta corriente negativa de lado bajo, no un control. Qué pin es el
  positivo lo fija el cobre.

---

## 5. Aparte del rework: el riel tiene que estar a 54 V

Independiente de la polaridad, pero bloquea antes que ella.

El firmware valida `Vin` **antes** de clasificar, contra la ventana de
SPoE Clase 13 (**50–58 V**). El ADC del LTC4296 lee **bajo**, con error de
**ganancia** de ~−2.5 %, y la dispersión entre placas es de ~600 mV:

| Placa | Fuente a 50.0 V | Fuente a 54 V |
|---|---|---|
| MFS | 49 455 mV | — |
| MPS | **48 825 mV** (falla) | **52 570 mV** (pasa) |

A 50.0 V se está en el borde exacto de la ventana y el error del ADC deja la
lectura fuera. **El riel de diseño para Clase 13 es ~54 V.** Operar ahí.

---

## 6. Verificación después del rework

En orden. Si un paso falla, no seguir al siguiente.

1. **Polaridad**: con `mps_hold_clasif.tcl`, el multímetro en el zócalo debe
   dar **+5 V** (antes −5 V).
2. **Línea SCCP**: en la misma pasada, `sccpi` debe pasar de 0 a **1** al
   entrar en SEARCHING. Ese es el indicador de que el módulo ya responde.
3. **Negociación**: con el firmware corriendo y el riel a 54 V, leer el estado
   con `tools/ltc4296-swd/mps_estado.tcl`. Debe verse:
   - `PxST` con **estado = 2** (`DELIVERING`) y **`POWERED = 1`**
   - `g_retry_rc` **distinto de 1** (un 2 es `SCCP_COMPLETE`)
   - `g_lg_any = 1`
4. **El PD arranca**: el field switch conectado debe encenderse solo.

### Indicadores que NO sirven, para no perder tiempo

- **El LED de PWR del PSM.** La ventana de clasificación dura 4 ms cada ~5 s:
  es invisible. Que no parpadee **no** significa que no esté clasificando.
- **`retry_rc = 1`.** `ADI_LTC_DISCONTINUE_SCCP` vale 1 y es un cajón de
  sastre: recoge Vin inválido, `PD_LINE_NOT_HIGH`, `PD_DETECTION_FAILED` y
  `PD_CRC_FAILED`. Solo `PD_NOT_PRESENT` (4) sale distinto. Un slot vacío y un
  puerto averiado dan el mismo código.
- **La tensión de sondeo `Vout`.** Da 5145 mV en todos los casos —puerto
  bueno, vacío y averiado— porque es la fuente de clasificación en circuito
  abierto.
- **`sccpi` en reposo.** Con un módulo insertado lee 0 **también en la placa
  sana**. Lo que discrimina es su valor **durante SEARCHING**.
