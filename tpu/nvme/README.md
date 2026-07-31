# SSD M.2 de la TPU: **el disco no estaba muerto, era la velocidad de enlace**

2026-07-31.

El KIOXIA BG4 de `flcon01` se había dado por perdido — llegó a existir un
drop-in de systemd con el comentario *"SSD M.2 muerto (KIOXIA BG4): BD
temporal en eMMC hasta reemplazar el SSD"*.

**No estaba muerto. Se cae a Gen2 y aguanta a Gen1.**

---

## 1. La evidencia

Prueba de integridad (`nvme_integridad.sh`): escribe 512 MB, los **relee sin
caché** (`O_DIRECT`, para que la lectura toque el disco de verdad y no la
caché de página) y compara SHA-256. En bucle.

| | Gen2 (5.0 GT/s) | Gen1 (2.5 GT/s) |
|---|---|---|
| Duración | murió a los **~7 min** | **20 min, 211 pasadas** |
| Errores | fallo de E/S | **0** |
| Estado final | controlador `dead`, fs remontado **solo lectura** | `live`, fs en `rw` |
| Tiempo por pasada | ~5.5 s | **~5.5 s** |

Unos **108 GB** escritos y verificados byte a byte sin un solo fallo a Gen1.

Kernel al caer a Gen2:

```
nvme 0001:04:00.0: Unable to change power state from D3cold to D0, device inaccessible
nvme nvme0: Disabling device after reset failure: -19
EXT4-fs (nvme0n1p1): Remounting filesystem read-only
```

## 2. Bajar a Gen1 **no cuesta ancho de banda**

Es el dato que hace fácil la decisión. La topología:

```
0001:00:00.0  BCM2712 root port      2.5 GT/s   <- el tramo de subida YA es Gen1
0001:01:00.0  ASM1182e upstream      2.5 GT/s
0001:02:07.0  ASM1182e -> NVMe       5.0 GT/s   <- el unico que iba a Gen2
0001:04:00.0  KIOXIA BG4             5.0 GT/s
```

El cuello de botella ya estaba aguas arriba, así que forzar Gen1 en el tramo
del NVMe **no mueve el límite**. Medido: mismo tiempo por pasada.

## 3. NO es gestión de energía

Se descartó con el `/proc/cmdline`, que ya trae todas las mitigaciones:

```
pcie_aspm=off  pcie_port_pm=off  nvme_core.default_ps_max_latency_us=0
```

y `/sys/bus/pci/devices/0001:04:00.0/power/control` = `on`. El mensaje de
`D3cold` sale del **intento de reset** del kernel tras el fallo, no de una
siesta: el kernel intenta recuperar el controlador y ya no lo encuentra.

## 4. Qué hay instalado

- `nvme-gen1.sh` → `/usr/local/sbin/` — fuerza Gen1. **Descubre el puerto a
  partir de `/sys/class/nvme/nvme0/device`** en vez de codificar el BDF, que
  puede cambiar si se reordena el árbol PCIe. Si ya está en Gen1, no toca nada.
- `nvme-gen1.service` → `/etc/systemd/system/` — `oneshot`,
  `Before=local-fs.target`, para que se aplique **antes de montar `/mnt/ssd`**
  y antes de que nadie escriba.

**Verificado con un corte de luz real** (habituales en Venezuela): el enlace
volvió a 5.0 GT/s y el servicio lo bajó solo.

```
nvme-gen1.sh: NVMe=0001:04:00.0  puerto=0001:02:07.0  velocidad antes: 5.0 GT/s PCIe
nvme-gen1.sh: velocidad despues: 2.5 GT/s PCIe
nvme-gen1.sh: OK: enlace del NVMe a Gen1
```

⚠️ **El ajuste NO sobrevive a un reinicio por sí solo.** Sin este servicio, el
disco arranca a Gen2 y falla al primer uso intenso. No quitarlo.

## 5. La pasarela vive ahora en el M.2

`store.path` = `/mnt/ssd/varec-gateway/varec.db`.

### Dos trampas al mover la base, las dos pisadas

**El WAL.** SQLite en modo WAL guarda lo reciente en el fichero `-wal` (había
2.2 MB). Copiar solo el `.db` **pierde lo último**. Parar el servicio antes
hace que se consolide solo; aun así conviene copiar `-wal` y `-shm`.

**Los drop-ins.** Renombrar `/var/lib/varec-gateway` dejó el servicio en
**`226/NAMESPACE`**: había un drop-in `emmc.conf` con
`ReadWritePaths=/var/lib/varec-gateway`, y con `ProtectSystem=strict` una ruta
inexistente en `ReadWritePaths` **impide montar el espacio de nombres**.
**Leer `systemctl cat` entero antes de mover nada** — el error fue truncarlo.

Ese drop-in ya se retiró, y con él vuelven las protecciones de la unidad base:

| directiva | para qué |
|---|---|
| `RequiresMountsFor=/mnt/ssd` | sin el disco montado **no arranca** |
| `BindPaths=/mnt/ssd` | garantiza que escribe en el disco real |
| `ReadWritePaths=/mnt/ssd /etc/varec-gateway` | no puede escribir en ningún otro sitio |

Sin ellas, si `/mnt/ssd` no montara, la pasarela arrancaría y escribiría en el
directorio subyacente de la eMMC — una base fantasma que quedaría **oculta**
en cuanto el disco volviera a montarse, aparentando pérdida de datos.

## 6. Cómo repetir la prueba

```bash
./nvme_integridad.sh [minutos] [MB_por_pasada]     # por defecto 15 y 512
# log en /mnt/ssd/nvme_integridad.log
```

⚠️ **Comprobar antes que no haya otro soak corriendo.** Hubo un
`@reboot ... nvme_soak.sh` en el crontab del usuario que duplicaba la carga e
invalidaba las comparaciones. Ya se quitó (respaldo en `~/crontab.bak-preclean`).
