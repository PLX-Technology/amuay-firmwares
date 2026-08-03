# Red del segmento SPE: **el segmento estaba puenteado con la red de oficina**

2026-08-03.

Este es el hallazgo que impedía que el sistema funcionara, y no era de
firmware.

---

## 1. El síntoma

Una ATT recién grabada quedaba muda a los dos minutos de arrancar. Estaba
**alimentada** (15 mA medidos en el ADC del puerto del LTC4296), su ranura
tenía **enlace con portadora y autonegociación completada**, su aplicación
seguía contando `tx ok` sin un solo fallo — y no llegaba nada a la pasarela.
Tampoco respondía al ARP ni al Modbus.

Su consola lo tenía escrito:

```
<err> net_pkt: Data buffer (1514) allocation failed.
<err> net_pkt: Data buffer (60) allocation failed.
189 x  OA RX: Data chunk not valid, skip !
```

Los 1514 bytes son una trama Ethernet de tamaño máximo: **tráfico de oficina**.
Los 60, el tamaño de un ARP — que es justo lo que dejó de contestar. Con el
fondo común de paquetes agotado se cae también la transmisión, y por eso
`tx ok` subía mientras no salía nada: la aplicación entregaba al socket y
debajo no había buffers.

## 2. La causa

**`pcie0` compartía dominio de difusión con la red corporativa (172.16.0.0/19).**

Evidencia directa, no deducción:

- La ATT arrancaba y su consola decía `net_dhcpv4: Received: 172.16.10.105` —
  dirección **del DHCP de la empresa**, no del rango del banco.
- Con un `dnsmasq` propio en `pcie0` se registró
  `DHCPACK(pcie0) 172.16.31.101 ... plvsrvdvr2`: **nuestro servidor
  contestando a una máquina Windows de la oficina**.
- `ip neigh` en `pcie0` listaba `172.16.0.1` (la puerta de enlace corporativa),
  `172.16.31.101` y `172.16.10.16`.

Un microcontrolador con unos pocos kilobytes para paquetes no puede vivir en
una red de oficina. No es un riesgo a futuro: mataba el nodo en dos minutos.

## 3. El arreglo: un cable

**Sacar el RJ45 del power switch del switch de oficina y llevarlo directo a la
TPU.**

Efecto medido en la ATT, en la misma ventana de 30 s de consola:

| | Antes | Después |
|---|---|---|
| `OA RX` descartados | **189** | **0** |
| Fallos de `net_pkt` | varios | **0** |
| Supervivencia | ~2 min | **>15 min** y contando |
| Paquetes recibidos en `pcie0` | 21317 | **~0,5/s** |

Una red de oficina nunca está a medio paquete por segundo. Esa es la
comprobación de que el aislamiento es real.

## 4. Reparto de interfaces en la TPU

| Interfaz | Driver | Papel |
|---|---|---|
| **`eth0`** | `macb` | **plano IP**: `192.168.50.1/24` + DHCP. Aquí está el RJ45 del MPS |
| **`pcie0`** | `lan743x` | **plano de datos**: socket `AF_PACKET` de la pasarela. **Sin dirección IP, y no le hace falta** |
| `wlan0` | — | salida a internet y SSH. **No se toca** |

El plano de datos no necesita IP: los niveles viajan en tramas Ethernet crudas
(ethertype `0x88B5`) y la pasarela las lee con un socket crudo. Solo el canal
de **configuración** (DHCP + Modbus TCP) necesita direccionamiento.

`eth0` enlaza a **100 Mbit**, que es exactamente lo que da el ADIN1300 del MPS
configurado en RMII. `pcie0` enlazaba a 1000, velocidad que ese PHY no negocia:
fue la pista de que el cable no estaba donde se creía.

⚠️ **Sin explicar:** `pcie0` recibe las tramas aunque el cable esté en `eth0`, y
ambas interfaces contaban lo mismo. No estorba a nada, pero está sin aclarar.

## 5. ⚠️ Trampa de `dnsmasq` que costó una hora

**Con `bind-interfaces`, `dnsmasq` responde SOLO por la interfaz a la que está
atado.** Atado a `pcie0` con el cable en `eth0`, el registro mostraba:

```
tags: known, pcie0
DHCPOFFER(pcie0) 192.168.50.11 02:00:70:22:30:5b
```

…y **nunca un `DHCPACK`**. La petición entraba, la oferta salía por el puerto
equivocado, y el cliente no la veía nunca.

**`DHCPOFFER` sin `DHCPACK` = interfaz de salida equivocada.** Vale la pena
recordarlo.

## 6. El DHCP se sirve **solo a MAC nominadas**

`dhcp_spe_restringido.sh` escribe:

```
dhcp-range=192.168.50.10,static,255.255.255.0,12h
dhcp-ignore=tag:!known
dhcp-host=02:00:70:22:30:5a,02:00:70:22:30:5b,192.168.50.11,att-tank1
dhcp-host=02:00:70:2d:30:08,02:00:70:2d:30:09,192.168.50.21,att-tank21
```

`static` + `dhcp-ignore` hacen que se **ignore todo lo que no esté en la
lista**. Aunque el aislamiento del cable no sea perfecto, no puede molestar a
la red de la empresa.

Las MAC se derivan del UID del STM32 y la placa usa la del **puerto 2**
(base+1), que es la que transmite. Se nominan las dos por si cambia el
cableado.

Direcciones fijas a propósito: para hablar Modbus con una ATT hay que saber
dónde está.

### ⚠️ Los respaldos NUNCA dentro de `/etc/dnsmasq.d`

`dnsmasq` lee **todos** los ficheros del directorio (solo ignora los sufijos
`.dpkg-*`), así que una copia de seguridad ahí duplica cada palabra clave:

```
dnsmasq: illegal repeated keyword at line 11 of .../varec-spe.conf.bak-...
```

Y no se ve venir: `dnsmasq --test` a secas da **OK** porque solo mira el
fichero. El `ExecStartPre` de Debian prueba con `-7` sobre el **directorio
entero**. Los scripts de aquí hacen esa misma prueba antes de reiniciar.

## 7. Para los 50 tanques

Misma topología multiplicada: **cada power switch con su RJ45 a un puerto
dedicado de la TPU, o a un switch propio del sistema que no sea el de la
oficina.** Con 3 power switches hacen falta tres puertos o un switch dedicado.

Y queda un problema de escala **propio**, independiente de la oficina: cada ATT
emite en **difusión** cada 500 ms. Con 50 tanques son 100 tramas por segundo
que **recibe cada ATT**, de las cuales 98 no le importan. No es ancho de banda
—son 6 kB/s sobre un enlace de 10 Mbit— sino reservar y tirar un buffer cien
veces por segundo, que es justo lo que ya sabemos que las mata.

Por orden de rendimiento:

1. **Bajar la cadencia.** Es gratis y ya es configurable (`HR 4`, en decenas de
   ms). Un nivel de tanque no se mueve a 2 Hz; a 5 s la carga se divide por
   diez.
2. **Dejar la difusión y mandar a la TPU en unicast.** ⚠️ Con una trampa: si el
   switch no ha aprendido la MAC de la TPU, el unicast desconocido se inunda
   igual. Habría que fijar la entrada estática en el ADIN6310.
3. **Filtrado por MAC en el ADIN2111**, que sabe hacerlo en hardware. Tira la
   basura antes de que llegue a la pila. No sustituye al aislamiento, pero da
   margen.

**Antes de comprometerse:** poblar un field switch con sus cinco ATT, dejarlo
horas con la cadencia final, y mirar los errores de `net_pkt` en la consola de
una de ellas. Si con cinco vecinos ya falla la reserva, con cincuenta no hay
discusión.
