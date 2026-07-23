# Auditoría: quemas del LTC4296 — firmware, esquemático y datasheet

> 2026-07-23. Revisión minuciosa solicitada tras dos LTC4296 destruidos en el
> power switch (MPS). Cubre: traza de **todas** las escrituras del firmware al
> chip (incluyendo escenarios de SPI degradado), verificación del esquemático
> contra el datasheet, y máximos absolutos. Conclusiones separadas por quema,
> sin sesgo: donde el firmware tiene responsabilidad plausible, se dice.

## Cronología y correlación

| Evento | Firmware corriendo | Resultado |
|---|---|---|
| Era pre-potencia | Original (driver por defecto, overlay APL) | **No quema** — con Vin=54V y clase APL (9.6-15V), `is_vin_valid` falla → el driver **nunca habilita puertos**. No energiza nada, no puede quemar nada. |
| Quema #1 (junto a VIN/CPO) | **Bring-up forzado** (bloque "(2) SOLUCION") | LTC4296 #1 destruido |
| Quema #2 (junto a R92) | **Seguro** (`pse_safe_class13`) | LTC4296 #2 destruido; el chip estaba **mudo por SPI desde el primer contacto con los 50 V** |

## Quema #1 — veredicto honesto: el firmware forzado ES un factor plausible

El bloque de bring-up escribía, incondicionalmente en cada arranque, en los 4
puertos: `GCFG|=TLIM_DISABLE`, `CFG1=0x013D` (SIG/PREBIAS override + TINRUSH=∞),
`CFG0=0x01E1/0x0FE1` (POWER_AVAILABLE forzado; la variante 0x0FE1 además
foldback y soft-start OFF). Contra el datasheet:

- Abs-max section: *"This IC includes **overtemperature protection** that is
  intended to protect the device during momentary overload"* → `TLIM_DISABLE`
  **apaga exactamente esa protección**.
- High-Side MOSFET Selection: el foldback ACL define el perfil SOA del sistema;
  *"Foldback can be disabled…"* pero el diseño del FET/sistema asume el perfil.
  Operar forzado, sin foldback ni timers, con el fallo activo del disyuntor de
  baja, es operación fuera de las suposiciones de diseño.

**Conclusión #1:** el chip trabajó a 54 V, forzado a entregar, sin límite
térmico ni timeouts, sobre una condición de fallo real (la línea de baja
disparando). Contribución del firmware: **plausible y no exonerable**. Nota:
también hizo falta la condición de fallo eléctrico subyacente — el firmware
quitó las redes de seguridad; no creó el fallo por sí solo.

## Quema #2 — veredicto: el firmware NO pudo causarla (probado a nivel de código)

Estado del chip: **mudo por SPI (todo `0xffff`) desde la primera lectura con
50 V puestos**, y quemado tras aplicar 50 V. Se auditó CADA camino de escritura
del firmware seguro, incluyendo el peor caso "MISO roto" (lecturas basura pero
escrituras SÍ llegan):

| Camino | Con lecturas `0xffff` | Escrituras resultantes |
|---|---|---|
| `probe()` al arrancar | unlock-verify falla → **aborta antes** de configurar puertos | `SW_RESET`, unlock (benignas) |
| Bucle retry → `do_spoe_sccp` | `is_port_disabled`: `0xffff&7=7≠DISABLED` → "ENABLED" → `is_port_deliver_pwr`: `7≠DELIVERING` → "UNKNOWN" | **`port_disable` = CFG0=0x0000 (APAGAR)** cada ~5 s. Única escritura. |
| Compuerta de Vin | `GADCDAT=0xffff` → bit12 "nuevo" activo → código 4095 → `(4095−2048)×35 = 71 645 mV` → **fuera de 50-58 V** → `vin_valid=false` → jamás habilita | ninguna |
| `set_port_pwr` (lo único que pone POWER_AVAILABLE) | inalcanzable: requiere Vin válido leído + prebias + classification + **handshake SCCP completo con un PD clasificado** (imposible con lecturas rotas) | ninguna |
| Re-arme lado bajo (`g_lg`) | gated en `estado==DELIVERING(2)`; `7≠2` | ninguna |
| Pin **AUTO = GND** (hardware) | al encender, TODOS los puertos deshabilitados y HGATEs apagados por diseño; el chip no puede auto-energizarse | — |

**Brecha real encontrada y divulgada (sin efecto en esta quema):** quedó un
bloque residual de diagnóstico ("(1) Línea SCCP") que hace
`prebias + en_and_classification` en el puerto 3 **sin validar Vin** — escrituras
incondicionales al arrancar. Impacto acotado: modo *classification* (SW_EN|
PSE_READY|SET_CLASSIFICATION, **sin** POWER_AVAILABLE) = corrientes de detección
de mA, no entrega de potencia; y en la placa #2 el chip estaba sin alimentar en
el arranque lógico (las escrituras cayeron en un chip muerto) y el bloque no se
re-ejecuta tras conectar los 50 V. **Se recomienda eliminarlo por higiene.**

**Conclusión #2:** con el firmware seguro, **ninguna ruta de código pudo
energizar un puerto** — ni siquiera con SPI a medias. Además el chip ya estaba
mudo desde el primer instante con 50 V: el daño ocurrió **al aplicar los 50 V o
antes**, con el firmware demostradamente pasivo (solo escribía "disable"). La
causa es de **hardware/manejo**, no de firmware.

## Esquemático vs datasheet (verificado con zoom, MPS_SCH)

| Elemento | Datasheet | Board | ✓/✗ |
|---|---|---|---|
| Cap CPO→IN | 1 nF, ≥16 V | C162 1000 pF/50 V a IN | ✓ |
| Bypass INT | 470 nF a GND | C163 0.47 µF | ✓ |
| SDO open-drain | pull-up requerido | R91 4.7k a 3.3 V | ✓ |
| CS | pull-up | R90 100k a 3.3 V | ✓ |
| AUTO | GND = modo host (puertos off al encender) | a GND | ✓ |
| Pin IN | directo al rail, bypass | directo a PSE_IN + C161 1 µF/100 V | ✓ |
| Sense alto R92/94/95/97 | según clase | 0.27 Ω | ✓ |
| **TVS D7** | *"TVS clamp voltage… must meet the maximum voltage ratings of the devices on the rail"* — **abs max IN = 80 V** | **SMBJ58A**: standoff 58 V, breakdown ≥64.4 V, **clamp hasta ~93 V** con surge fuerte | **⚠️ condicional**: con un surge/hot-plug enérgico el clamp supera los 80 V del chip |

## Mecanismo de hardware más consistente con la quema #2

**Transitorio de energización (hot-plug de los 50 V):** conectar una fuente viva
a través del cable (inductancia) contra los ceramicos de entrada produce un
ring que puede acercarse a 2×Vin (~100 V); el SMBJ58A recorta, pero con energía
suficiente su clamp (hasta ~93 V) **excede los 80 V abs max de IN**. Encaja con:
chip mudo desde el primer contacto con 50 V (dañado al energizar), quema #1 en
la zona VIN/CPO, quema #2 en el camino de entrada (R92), y con que la era del
firmware original no quemara (si entonces la fuente se rampeaba o el chip nunca
se estresó por no energizar puertos). *Otras posibles: inversión/miswire
momentáneo al conectar, unidad defectuosa.*

## Experimento decisivo (zanja la discusión de culpas)

Energizar los 50 V con el **MAX32690 en reset/halt por SWD** (cero SPI — el
firmware provadamente fuera de la ecuación), fuente **limitada a ~200 mA** y
**rampeada desde 0 V** con el cable pre-conectado, y un **osciloscopio en el pin
IN** durante la energización:
- Si el LTC sobrevive rampeado pero muere con hot-plug → transitorio de entrada.
- Si muere incluso rampeado con el MCU en reset → hardware puro, sin discusión.
- Si sobrevive todo → recién ahí revisitar firmware con la reproducción en mano.

## Recomendaciones

1. **Nunca hot-plug de los 50 V**: cable pre-conectado + rampear la fuente desde
   0 V (primera vez con límite de corriente).
2. Osciloscopio en IN durante energizaciones hasta cerrar el root-cause.
3. Revisar el TVS según la regla del datasheet (clamp < 80 V al surge esperado;
   considerar SMCJ de mayor Ipp o clamp menor).
4. Firmware: eliminar el bloque residual "(1) Línea SCCP" (higiene) y mantener
   SIEMPRE las protecciones por defecto (nunca reusar el config de banco).
5. Reponer el próximo LTC4296 solo después del experimento decisivo.
