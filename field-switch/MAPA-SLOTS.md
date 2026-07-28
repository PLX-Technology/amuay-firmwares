# Field switch (MFS) — mapa de slots: datos, potencia y straps

> Leído del esquemático `Downloads/MFS.png`, slot por slot (nets de cada
> conector). Verificado contra los casos comprobados empíricamente con el PDM.

## Tabla

| Slot (serigrafía) | Conector | Nets de datos | **Strap MDIO** | Potencia (LTC port) |
|---|---|---|---|---|
| **Port 1** | J1 | `P2_*` | **2** | *(uplink: el PDM recibe PoDL, `PSE_OUT` alimenta el rail)* |
| **Port 2** | J3 | `P1_*` | **1** | LTC 0 |
| **Port 3** | J4 | `P0_*` | **0** | LTC 1 |
| **Port 4** | J5 | `P5_*` | **5** | LTC 2 |
| **Port 5** | J6 | `P4_*` | **4** | LTC 3 |
| **Port 6** | J8 | `P3_*` | **3** | LTC 4 |

## Reglas

1. **El strap MDIO del módulo = el nº de macPort de DATOS del slot.** El
   firmware configura `phyAddr = nº de macPort` en `initializePorts_p`.
2. **Los números de datos y de potencia NO coinciden en el mismo slot.**
   Ej.: slot Port 4 = datos `macPort5` + potencia `LTC port 2`. Mirar SIEMPRE
   los nets del esquemático, nunca la serigrafía.
3. **El firmware solo instancia 4 puertos PSE** (`port0..port3` del driver =
   LTC 0-3 = slots Port 2 a Port 5). El **slot Port 6** (LTC port 4) queda
   deshabilitado salvo que se añada el 5º puerto (ver
   `mfs-short-detect/README.md`).
4. Confirmación del pin de selección SPI: el slot Port 4 lleva `PORT_SS_P5`,
   consistente con `macPort5`.

## Síntoma de strap incorrecto

El enlace puede **entrenar igual** (el ADIN1100 linkea con sus defaults de
hardware), pero el switch **no ve el PHY** (`SES_ReadPhyReg` devuelve `0xffff`)
y **no cruza ni una trama**. Ver `PDM-SPE-NOTES.md`.

⚠️ **El link-up NO prueba que pasen datos**, y a la inversa: el strap correcto
tampoco garantiza enlace (ver el caso abierto de la ATT en
`ATT-PODL-DIAG.md`, donde el strap 5 es correcto y aun así no hay portadora).
