#!/usr/bin/env bash
# =============================================================================
#  provision-tpu-pws.sh
#  Provisiona un equipo VIRGEN: TPU (Raspberry Pi CM5 + placa portadora) + PWS
#  (power switch ADIN6310 / MAX32690).
#
#  Consolida TODO el proceso validado:
#    FASE 0  Clon del SO SD/SSD -> eMMC  (guiado, se corre en la consola del CM5)
#    FASE 1  Drivers de red parcheados (lan743x, adin1110/adin1100)  [SSH]
#    FASE 2  Overlays de config.txt + curva de fan + EEPROM           [SSH]
#    FASE 3  Flasheo del firmware adin_phyfix al MAX32690 del PWS      [OpenOCD local]
#    FASE 4  Habilitacion + verificacion de puertos (SPE/RS232/RS485/eth) [SSH]
#    FASE 5  Flasheo de la tarjeta ATT (STM32WBA65) por COM6           [local]
#
#  Se ejecuta DESDE el host Windows (Git Bash):
#    - Los pasos del CM5 van por SSH.
#    - El flasheo del PWS usa OpenOCD/sign_app/send_scp LOCALES (MaximSDK).
#    - Los pasos fisicos (POR, quitar SD, hardware) se pausan y piden accion.
#
#  Uso:
#    ./provision-tpu-pws.sh            # muestra el menu de fases
#    ./provision-tpu-pws.sh 1 2 4      # corre fases 1,2 y 4
#    ./provision-tpu-pws.sh all        # corre 1..4 (la 0 es manual, ver abajo)
#
#  IMPORTANTE: editar la seccion CONFIG antes de usar en cada equipo.
#              Cada TPU necesita una MAC de ADIN1110 UNICA (CM5_MAC_ADIN).
# =============================================================================
set -uo pipefail

# ============================== CONFIG =======================================
# ---- Acceso al CM5 (TPU) ----
CM5_IP="${CM5_IP:-172.16.10.109}"                       # IP del CM5 (por WiFi wlan0)
CM5_USER="tpu01"                             # usuario del CM5
# Credenciales: NUNCA van en el repo. Se leen de tools/provision.env
# (que esta en .gitignore) o del entorno. Ver provision.env.example.
_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$_here/provision.env" ] && . "$_here/provision.env"
CM5_PASS="${CM5_PASS:-}"                     # clave sudo del CM5 (de provision.env)
CM5_SSH_METHOD="key"                         # "key" (ssh -i) o "plink" (PuTTY -pw)
CM5_KEY="/c/Users/joseh/.ssh/id_ed25519"     # llave privada (si method=key)
CM5_HOSTKEY=""                               # fingerprint SHA256 (si method=plink)

# ---- Identidad UNICA por equipo (CAMBIAR en cada unidad) ----
CM5_HOSTNAME="flcon01"                        # hostname del CM5
CM5_MAC_ADIN="02:00:00:ad:11:10"              # MAC del ADIN1110 (SPE). ¡UNICA por unidad!

# ---- Artefactos (rutas en el host Windows) ----
# Carpeta con los .ko parcheados, el overlay .dtbo y el firmware firmado.
ART_DIR="/c/Users/joseh/tpu-artifacts"        # <-- ajustar
KO_LAN743X="$ART_DIR/lan743x.ko"              # driver LAN7431 parcheado (fixed-link)  -> eth (RGMII)
KO_ADIN1110="$ART_DIR/adin1110.ko"            # SPE MAC-PHY  -> eth2
KO_ADIN1100="$ART_DIR/adin1100.ko"            # PHY interno del ADIN1110
KO_DP83869="$ART_DIR/dp83869.ko"              # PHY del puerto FIBRA (LAN7801->DP83869->SFP)
DTBO_ADIN="$ART_DIR/adin1110-bitbang.dtbo"    # se regenera con la MAC del equipo (ver FASE 1)
DTS_ADIN="$ART_DIR/adin1110-bitbang.dts"      # fuente del overlay (para inyectar la MAC)

# ---- Firmware del PWS (MAX32690/ADIN6310) ----
# adin_phyfix = loopfix-v2 + FIX 2026-07-15 de direcciones MDIO (ver FASE 3).
# Reemplaza a adin_loopfix2.sbin, que dejaba el RJ45 muerto.
PWS_SBIN="$ART_DIR/adin_phyfix.sbin"          # firmware del PWS, FIRMADO (test key)
PWS_JUMP="0x10006258"                         # jump_address usado al firmar (= __start)

# ---- Firmware de la ATT (STM32WBA65 + ADIN2111) ----
# att_bypass = ADIN2111 por SPI bit-bang + ACTIVA EL RELE DE BYPASS (ver FASE 5).
ATT_BIN="$ART_DIR/att_bypass.bin"             # binario RAW (el WBA65 NO usa secure boot)
ATT_COM="COM6"                                # FT232 de la ATT (USART1 PB12/PA8)
ATT_FLASHER="$ART_DIR/stm32flash.py"          # flasher AN3155 con reintentos por bloque
PY_WIN="python"                               # Python de Windows (necesita pyserial)

# ---- Herramientas de flasheo (MaximSDK, en Windows) ----
OPENOCD="/c/MaximSDK/Tools/OpenOCD/openocd.exe"
OPENOCD_SCRIPTS="C:/MaximSDK/Tools/OpenOCD/scripts"
SBT_DIR="C:/MaximSDK/Tools/SBT"
SSCP_DIR="$ART_DIR/sscp"                       # send_scp.py parcheado (bug ord() Py3)
CRK_ZIP="$SBT_DIR/devices/MAX32690/scp_packets/writemaximcrk.zip"
PICO_COM="COM5"                               # UART del MAX32625PICO (para SCP de la CRK)

# ---- Kernel del CM5 (para rutas de modulos) ----
KREL="6.18.34+rpt-rpi-2712"

# =============================================================================
# ============================== HELPERS ======================================
c_ok(){ printf "\033[32m[OK]\033[0m %s\n" "$*"; }
c_info(){ printf "\033[36m[..]\033[0m %s\n" "$*"; }
c_warn(){ printf "\033[33m[!!]\033[0m %s\n" "$*"; }
c_err(){ printf "\033[31m[XX]\033[0m %s\n" "$*"; }
hr(){ printf '%s\n' "-------------------------------------------------------------------"; }

pause_manual(){  # pausa para un paso fisico
  echo; c_warn "ACCION MANUAL: $1"
  read -rp "    >>> Cuando este listo, pulsa Enter para continuar (Ctrl-C aborta)... " _
}

# Ejecuta un comando en el CM5 por SSH (stdin = script bash)
cm5(){  # uso: cm5 <<'EOF' ... EOF
  if [ "$CM5_SSH_METHOD" = "plink" ]; then
    plink -batch -hostkey "$CM5_HOSTKEY" -pw "$CM5_PASS" "$CM5_USER@$CM5_IP" "bash -s"
  else
    ssh -o BatchMode=yes -o ConnectTimeout=10 -i "$CM5_KEY" "$CM5_USER@$CM5_IP" "bash -s"
  fi
}

# Copia un archivo al CM5 (a /tmp) e imprime la ruta destino
cm5_put(){  # uso: cm5_put <archivo_local>  -> /tmp/<basename>
  local f="$1" b; b="$(basename "$f")"
  if [ "$CM5_SSH_METHOD" = "plink" ]; then
    pscp -batch -hostkey "$CM5_HOSTKEY" -pw "$CM5_PASS" "$f" "$CM5_USER@$CM5_IP:/tmp/$b" >/dev/null
  else
    scp -o BatchMode=yes -i "$CM5_KEY" "$f" "$CM5_USER@$CM5_IP:/tmp/$b" >/dev/null
  fi
  echo "/tmp/$b"
}

need_creds(){
  [ -n "$CM5_PASS" ] && return 0
  c_err "Falta CM5_PASS. Crea tools/provision.env (ver provision.env.example) o exportala."
  return 1
}

check_cm5(){
  need_creds || return 1
  if echo "echo up" | cm5 2>/dev/null | grep -q up; then c_ok "CM5 alcanzable ($CM5_IP)"; return 0
  else c_err "CM5 NO alcanzable en $CM5_IP (¿encendido? ¿WiFi? ¿fase 0 hecha?)"; return 1; fi
}

wait_cm5(){  # espera a que el CM5 vuelva tras un reboot
  c_info "Esperando a que el CM5 vuelva..."; sleep 25
  for _ in $(seq 1 20); do
    if echo "echo up" | cm5 2>/dev/null | grep -q up; then c_ok "CM5 arriba"; return 0; fi
    sleep 6
  done
  c_err "El CM5 no volvio a tiempo"; return 1
}

need_file(){ [ -f "$1" ] || { c_err "Falta artefacto: $1"; return 1; }; }

# =============================================================================
# FASE 0 -- CLON DEL SO SD/SSD -> eMMC  (GUIA; se corre en la consola del CM5)
# El CM5 aun no es alcanzable por red, asi que esta fase solo IMPRIME los pasos.
# =============================================================================
fase0_guia(){
  hr; c_info "FASE 0 — Instalar el SO en la eMMC (manual, en la consola del CM5)"; hr
  cat <<'GUIA'
  Objetivo: dejar Pi OS Lite (Debian trixie, kernel 6.18.x) en la eMMC del CM5.

  A) Arrancar el CM5 desde medio EXTERNO (SD o USB con Pi OS/Ubuntu) para ver la eMMC.
     - eMMC = /dev/mmcblk0 ; el medio externo (SD/SSD USB) = /dev/sda o /dev/mmcblk1.
       Confirmar con:  lsblk -o NAME,SIZE,TRAN,MODEL

  B) OPCION 1 — Imagen fresca (recomendado, unidad virgen):
       xzcat pi-os-lite.img.xz | sudo dd of=/dev/mmcblk0 bs=4M conv=fsync status=progress
       sync
     OPCION 2 — Clonar desde la fuente (SD/SSD) reduciendo tamano:
       - Copiar tabla de particiones conservando disk-id, mkfs.vfat (boot) + mkfs.ext4 (root),
         rsync -aHAXx de la fuente a la eMMC (mantener PARTUUID en cmdline/fstab).

  C) Primer arranque desde eMMC:
       - APAGAR, QUITAR la SD/USB (PARTUUID identicos), ENCENDER -> arranca de la eMMC.

  D) Configurar red y acceso (en consola, Pi OS trixie NO aplica el custom.toml):
       sudo nmcli dev wifi connect "$WIFI_SSID" password "$WIFI_PASS"   # ver provision.env
       sudo nmcli con mod <perfil-wifi> connection.autoconnect yes
       sudo raspi-config nonint do_hostname <HOSTNAME>
       sudo systemctl enable ssh --now
       # (opcional) IP fija por nmcli si no se quiere DHCP

  Cuando el CM5 tenga IP y SSH, actualizar CM5_IP en este script y seguir con la FASE 1.
GUIA
  pause_manual "Completa la FASE 0 en el CM5 y confirma que ya responde por SSH."
  check_cm5
}

# =============================================================================
# FASE 1 -- DRIVERS DE RED PARCHEADOS (lan743x, adin1110/adin1100)
# Mismo kernel -> se COPIAN los .ko precompilados (no hace falta recompilar).
# =============================================================================
fase1_drivers(){
  hr; c_info "FASE 1 — Instalando drivers parcheados en el CM5"; hr
  check_cm5 || return 1
  need_file "$KO_LAN743X" || return 1
  need_file "$KO_ADIN1110" || return 1
  need_file "$KO_ADIN1100" || return 1
  need_file "$KO_DP83869" || return 1
  need_file "$DTS_ADIN"    || return 1

  # Inyectar la MAC unica del equipo en el .dts y regenerar el .dtbo localmente si hay dtc,
  # o subir el .dts y compilarlo en el CM5 (que si tiene dtc).
  c_info "Preparando overlay adin1110-bitbang con MAC $CM5_MAC_ADIN"
  local dts_local="/tmp/adin1110-bitbang.$CM5_HOSTNAME.dts"
  sed -E "s/local-mac-address = \[[0-9a-fA-F ]+\]/local-mac-address = [${CM5_MAC_ADIN//:/ }]/" \
      "$DTS_ADIN" > "$dts_local"

  local rk rd rd2 rd3 rdts
  rk="$(cm5_put "$KO_LAN743X")"
  rd="$(cm5_put "$KO_ADIN1110")"
  rd2="$(cm5_put "$KO_ADIN1100")"
  rd3="$(cm5_put "$KO_DP83869")"
  rdts="$(cm5_put "$dts_local")"

  cm5 <<EOF
set -e
S(){ echo "$CM5_PASS" | sudo -S -p "" "\$@"; }
KREL="\$(uname -r)"
UPD="/lib/modules/\$KREL/updates"
S mkdir -p "\$UPD"
S cp "$rk"  "\$UPD/lan743x.ko"
S cp "$rd"  "\$UPD/adin1110.ko"
S cp "$rd2" "\$UPD/adin1100.ko"
S cp "$rd3" "\$UPD/dp83869.ko"     # PHY del puerto FIBRA
S depmod -a
# dp83869 debe cargar ANTES de que enumere el LAN7801 -> a /etc/modules
S bash -c 'grep -q "^dp83869\$" /etc/modules || echo dp83869 >> /etc/modules'
echo "[cm5] modulos copiados (incl. dp83869) + depmod + /etc/modules"
# compilar el overlay con la MAC del equipo
S dtc -@ -I dts -O dtb -o /boot/firmware/overlays/adin1110-bitbang.dtbo "$rdts" 2>/dev/null \
  && echo "[cm5] overlay adin1110-bitbang.dtbo instalado (MAC $CM5_MAC_ADIN)" \
  || echo "[cm5] AVISO: dtc fallo; subir el .dtbo ya compilado a /boot/firmware/overlays/"
# regenerar initramfs para que cargue lan743x desde updates/ (critico)
S update-initramfs -u -k "\$KREL"
echo "[cm5] initramfs regenerado"
EOF
  c_ok "FASE 1 completa (drivers + overlay SPE)"
}

# =============================================================================
# FASE 2 -- config.txt (overlays + curva de fan) + EEPROM (fan poweroff)
# =============================================================================
fase2_config(){
  hr; c_info "FASE 2 — config.txt overlays + curva fan + EEPROM"; hr
  check_cm5 || return 1

  # Editor de config.txt en Python (robusto: sin awk/tee con comillas anidadas).
  # Inserta la curva de fan ANTES del 1er dtoverlay y agrega los overlays que falten.
  local pyed; pyed="$(mktemp)"
  cat > "$pyed" <<'PYEOF'
cfg = "/boot/firmware/config.txt"
lines = open(cfg).read().splitlines()
fan = ["# --- Curva ventilador TPU (provision) ---",
 "dtparam=fan_temp0=1000","dtparam=fan_temp0_hyst=1000","dtparam=fan_temp0_speed=100",
 "dtparam=fan_temp1=55000","dtparam=fan_temp1_hyst=5000","dtparam=fan_temp1_speed=140",
 "dtparam=fan_temp2=65000","dtparam=fan_temp2_hyst=5000","dtparam=fan_temp2_speed=190",
 "dtparam=fan_temp3=72000","dtparam=fan_temp3_hyst=5000","dtparam=fan_temp3_speed=255"]
if not any("fan_temp0=1000" in l for l in lines):
    idx = next((i for i,l in enumerate(lines) if l.startswith("dtoverlay=")), len(lines))
    lines = lines[:idx] + fan + lines[idx:]     # dtparam ANTES del 1er dtoverlay (trampa conocida)
    print("fan curve insertada")
for ov in ["adin1110-bitbang","lan7431-fixed-link","uart4-pi5","uart2-pi5"]:
    if not any(l.strip() == "dtoverlay="+ov for l in lines):
        lines.append("dtoverlay="+ov); print("+ dtoverlay="+ov)
open(cfg,"w").write("\n".join(lines)+"\n")
print("config.txt actualizado")
PYEOF
  local rpy; rpy="$(cm5_put "$pyed")"; rm -f "$pyed"

  cm5 <<EOF
S(){ echo "$CM5_PASS" | sudo -S -p "" "\$@"; }
S cp /boot/firmware/config.txt "/boot/firmware/config.txt.bak-provision-\$(date +%s)"
S python3 "$rpy"
# EEPROM: mantener RP1 vivo en halt -> el fan NO se dispara al apagar (usar 'sudo bash -c' para el redirect)
S rpi-eeprom-config --out /tmp/ee.conf >/dev/null 2>&1
S bash -c 'grep -q POWER_OFF_ON_HALT=0 /tmp/ee.conf || echo POWER_OFF_ON_HALT=0 >> /tmp/ee.conf'
S rpi-eeprom-config --apply /tmp/ee.conf >/dev/null 2>&1 && echo "EEPROM POWER_OFF_ON_HALT=0 aplicado"
echo "--- config.txt (overlays/fan) ---"; grep -nE "dtoverlay|fan_temp0" /boot/firmware/config.txt
EOF

  c_warn "Se necesita REINICIAR el CM5 para aplicar overlays/EEPROM."
  read -rp "    >>> ¿Reiniciar el CM5 ahora? [y/N] " a
  if [ "${a:-N}" = "y" ]; then
    echo "echo $CM5_PASS | sudo -S -p '' reboot" | cm5 2>/dev/null || true
    wait_cm5
  fi
  c_ok "FASE 2 completa"
}

# =============================================================================
# FASE 3 -- FLASHEO DEL PWS (MAX32690) con adin_phyfix  [OpenOCD LOCAL en Windows]
#   NOTA: el .sbin ya viene FIRMADO con la test key. Las placas nuevas suelen
#   traer la CRK test key de fabrica (writemaximcrk da code=0xa = "ya existe" = OK).
#
#   ★ FIX 2026-07-15 incluido en adin_phyfix.sbin (initializePorts_p):
#     El ADIN6310 tiene UN SOLO bus MDIO compartido: ADIN1300 del RJ45 (U4) y los
#     4 modulos SPE cuelgan del mismo par MDC/MDIO (R20/R21 = 0 ohm).
#       - El ADIN1300 esta en la direccion MDIO **1** (PHYID 0x0283/0xBC30).
#       - loopfix-v2 ponia port5 (RJ45) en addr **0** -> leia 0xffff -> RJ45 MUERTO,
#         y ademas port1 en addr **1** -> le pisaba la config al ADIN1300.
#       - Corregido: port5 -> addr 1 ; port1 -> addr 9 (libre).
#     ==> RESTRICCION: ningun modulo SPE puede usar DIP = 1 (choca con el RJ45).
#
#   ★ g_link / SES_GetLinkState NO ES EVIDENCIA: una direccion MDIO vacia se lee
#     0xffff por el pull-up del bus y SES lo reporta como link=1. Para saber si un
#     puerto SPE linkea DE VERDAD, leer 1.08F7 bit0 (B10L_STAT) del PHY.
# =============================================================================
fase3_flash_pws(){
  hr; c_info "FASE 3 — Flasheo adin_phyfix al MAX32690 del PWS"; hr
  need_file "$PWS_SBIN" || return 1
  [ -x "$OPENOCD" ] || { c_err "OpenOCD no encontrado: $OPENOCD"; return 1; }

  pause_manual "Conecta la sonda MAX32625PICO al header J11 (uC SWD) del PWS y alimenta la placa."

  # (Opcional) Provisionar/verificar la CRK test key por SCP (solo si el chip esta virgen de CRK).
  # Requiere COM5 (UART del PICO) y hacer un POWER-CYCLE mientras send_scp escucha.
  # Si la placa ya trae la test key, ESTE PASO NO HACE FALTA (dara code=0xa).
  if [ -d "$SSCP_DIR" ] && [ -f "$CRK_ZIP" ]; then
    read -rp "    >>> ¿Intentar provisionar la CRK test key por SCP? (normalmente NO) [y/N] " q
    if [ "${q:-N}" = "y" ]; then
      c_info "Escuchando SCP en $PICO_COM 30s — haz un POWER-CYCLE del PWS ahora"
      ( cd "$SSCP_DIR" && MAXIM_SBT_DIR="${SBT_DIR//\//\\}" \
        python -W ignore send_scp.py -c MAX32690 -s "$PICO_COM" -i uart -x "$CRK_ZIP" -t 30 ) \
        2>&1 | tr -d '|/-\\' | grep -aiE "success|exist|module|error|crk" | tail -5 || true
      c_info "(code=0xa 'exist' = la CRK ya estaba -> OK para seguir)"
    fi
  fi

  # Borrar flash (ayuda) + programar el firmware firmado + verificar
  c_info "Programando $PWS_SBIN en 0x10000000 ..."
  "$OPENOCD" -s "$OPENOCD_SCRIPTS" -f interface/cmsis-dap.cfg -f target/max32690.cfg \
    -c "init; reset halt; program \"$PWS_SBIN\" 0x10000000 verify reset exit" 2>&1 \
    | grep -aiE "programming|verified|error" || true
  # "checksum mismatch - attempting binary compare" seguido de "Verified OK" es NORMAL.

  pause_manual "Haz un POR REAL (power-cycle) del PWS. El secure-boot solo corre en POR."
  c_info "Esperando ~35s a que arranque (carga el blob al ADIN6310 por SPI)..."; sleep 35

  # Verificar que el PC quedo en FLASH (0x10000xxx) = firmware corriendo
  c_info "Verificando arranque por SWD (PC debe estar en 0x10000xxx)..."
  local pc
  pc="$("$OPENOCD" -s "$OPENOCD_SCRIPTS" -f interface/cmsis-dap.cfg -f target/max32690.cfg \
        -c "init; halt; resume; exit" 2>&1 | grep -aoiE "pc: 0x[0-9a-f]+" | head -1)"
  echo "    $pc"
  case "$pc" in
    *0x10*) c_ok "PWS ARRANCO (firmware en flash). loopfix-v2 corriendo." ;;
    *)      c_warn "PC no esta en flash. Si es 0x00002xxx = sigue en ROM: repetir POR y esperar mas, o revisar CRK." ;;
  esac
  c_warn "SPE: los modulos de ESTA placa pueden tener direcciones MDIO distintas."
  c_warn "adin_phyfix espera modulos en addr 2 (port2) y 4 (port4); port1->9 y port3->3 quedan libres."
  c_warn "La direccion la fija el DIP de CADA MODULO, y NUNCA puede ser 1 (ahi vive el ADIN1300 del RJ45)."
  c_warn "Si un puerto SPE no linkea: escanear el bus (main.c trae el bloque g_scan; ojo, hace"
  c_warn "  solo clause-45 -> el ADIN1300, que es clause-22, aparece como 0x0000, NO como 0xffff)"
  c_warn "  y ajustar initializePorts_p + recompilar + re-firmar con jump_address=$PWS_JUMP."
}

# =============================================================================
# FASE 5 -- FLASHEO DE LA ATT (STM32WBA65 + ADIN2111)  [bootloader UART, COM6]
#   La ATT es la tarjeta que va DENTRO del sensor Varec 2500: lee el encoder y
#   transmite la medida por SPE al PWS. NO usa secure boot -> binario RAW.
#
#   ★ CLAVE (2026-07-15) — RELE DE BYPASS: la ATT trae un rele K1 (Omron G6K-2F-Y)
#     en el bloque "10BASE-T1L Connections", gobernado por Q2 (BSS138) cuya
#     compuerta es UC_BYPASS_EN = pin 37 = **PD14**. R44 la mantiene en BAJO, asi
#     que el rele arranca DESENERGIZADO = EN BYPASS: puentea PORT1 con PORT2 y deja
#     el ADIN2111 FUERA de la linea SPE (los puentes SJ1/SJ3 no estan poblados).
#     ==> El firmware DEBE poner PD14 en ALTO al arrancar, o NO hay link jamas:
#         gpio_pin_configure(gpiod, 14, GPIO_OUTPUT_ACTIVE);  k_msleep(50);
#     Sintoma si falta: ambos PHY sanos (fuera de power-down, AN ON, TX 2.4V) pero
#     ceguera MUTUA -> 7.0207 (AN_T1_LP_H) = 0x0000 en los DOS extremos.
# =============================================================================
fase5_flash_att(){
  hr; c_info "FASE 5 — Flasheo de la ATT (STM32WBA65) por $ATT_COM"; hr
  need_file "$ATT_BIN"     || return 1
  need_file "$ATT_FLASHER" || return 1

  pause_manual "Conecta la ATT por USB (aparece como $ATT_COM) y entra en BOOTLOADER: manten el boton 'uC Bootload' (BOOT0) + reset, y suelta."

  # El bootloader ROM es INTERMITENTE: falla ~1 de cada 2 con "write FALLO en offset 0x..".
  # La placa SIGUE en bootloader tras un fallo -> basta reintentar.
  local ok=0 i
  for i in 1 2 3 4; do
    c_info "Intento $i/4 de flasheo..."
    if "$PY_WIN" "$ATT_FLASHER" "$ATT_BIN" 2>&1 | tail -2 | grep -q "FLASHEO COMPLETO"; then
      ok=1; break
    fi
    c_warn "Fallo el bloque (glitch del bootloader). Reintentando; la placa sigue en bootloader."
  done
  [ "$ok" = 1 ] || { c_err "No se pudo flashear la ATT tras 4 intentos. ¿Sigue en bootloader? ¿$ATT_COM libre?"; return 1; }
  c_ok "ATT flasheada y ejecutando (Go 0x08000000)"

  # Verificacion: la consola es 8N1 (la app NO usa paridad; el bootloader si es 8E1).
  c_info "Leyendo consola de la ATT ($ATT_COM 115200 8N1) 14s..."
  local rd; rd="$(mktemp)"
  cat > "$rd" <<'PYEOF'
import serial, sys, time
ser = serial.Serial('COM6', 115200, timeout=0.4)
t0 = time.time(); buf = b''
while time.time() - t0 < 14:
    d = ser.read(512)
    if d: buf += d
ser.close()
txt = buf.decode('utf-8', 'replace')
import re
txt = re.sub(r'\x1b\[[0-9;]*m', '', txt)
print(txt[-1800:] if txt else '(0 bytes - sin salida)')
PYEOF
  sed -i "s/'COM6'/'$ATT_COM'/" "$rd"
  "$PY_WIN" "$rd" 2>&1 | grep -aE "BYPASS|carrier|LINK|ready|ADIN" | tail -8
  rm -f "$rd"

  hr
  c_info "Que esperar si el enlace SPE quedo bien:"
  c_info "  'UC_BYPASS_EN (PD14) = 1 -> rele K1 energizado, ADIN2111 EN LINEA'"
  c_info "  'iface 1 dev=port_0  up=1 carrier=1   <<<<< LINK SPE!!!'"
  c_info "Confirmacion desde el PWS (por SWD, opcional): el PHY del puerto SPE usado"
  c_info "  debe pasar 1.08F7 -> 0x2801 (bit0=link) y 7.0201 -> 0x002c (bit5=AN completa)."
  c_warn "Si carrier sigue en 0: revisar que el cable SPE este en un puerto del PWS CON modulo,"
  c_warn "  y que el modulo encienda su LED (LED = LINKUP; si no enciende con NINGUN cable,"
  c_warn "  ese modulo/slot esta averiado -> probarlo en otro slot para aislar)."
  c_ok "FASE 5 completa"
}

# =============================================================================
# FASE 4 -- HABILITAR + VERIFICAR PUERTOS (eth/SPE/RS232/RS485/fan)
# =============================================================================
fase4_puertos(){
  hr; c_info "FASE 4 — Habilitando/verificando puertos del CM5"; hr
  check_cm5 || return 1

  cm5 <<EOF
S(){ echo "$CM5_PASS" | sudo -S -p "" "\$@" 2>/dev/null; }
echo "=== Interfaces de red (identificar por DRIVER, los nombres eth1/eth2 se intercambian) ==="
for i in eth0 eth1 eth2; do
  drv=\$(S ethtool -i \$i 2>/dev/null | awk '/^driver/{print \$2}')
  [ -n "\$drv" ] && echo "  \$i -> \$drv (ADIN1110=SPE, lan743x=LAN7431, macb=onboard)"
done
# levantar SPE (ADIN1110) y LAN7431 — buscar por driver
for i in eth0 eth1 eth2; do
  drv=\$(S ethtool -i \$i 2>/dev/null | awk '/^driver/{print \$2}')
  case "\$drv" in ADIN1110|lan743x) S ip link set \$i up ;; esac
done
sleep 3
echo "=== SPE (ADIN1110): link ? ==="
for i in eth0 eth1 eth2; do
  drv=\$(S ethtool -i \$i 2>/dev/null | awk '/^driver/{print \$2}')
  [ "\$drv" = "ADIN1110" ] && { echo -n "  \$i: "; S ethtool \$i 2>/dev/null | grep -iE "link detected|speed|master-slave status" | tr '\n' ' '; echo; }
done

echo "=== RS-232 = /dev/ttyAMA4 (UART4) ==="
ls -l /dev/ttyAMA4 2>/dev/null && S stty -F /dev/ttyAMA4 115200 cs8 -cstopb -parenb raw -echo && echo "  ttyAMA4 configurado 115200 8N1" || echo "  FALTA ttyAMA4 (¿overlay uart4-pi5? ¿reboot?)"
echo "=== RS-485 = /dev/ttyAMA2 (UART2) ==="
ls -l /dev/ttyAMA2 2>/dev/null && S stty -F /dev/ttyAMA2 115200 cs8 -cstopb -parenb raw -echo && echo "  ttyAMA2 configurado 115200 8N1" || echo "  FALTA ttyAMA2 (¿overlay uart2-pi5? ¿reboot?)"

echo "=== Fan (control termico) ==="
echo "  trip points: \$(cat /sys/class/thermal/thermal_zone0/trip_point_*_temp 2>/dev/null | tr '\n' ' ')"
echo "  temp actual: \$(( \$(cat /sys/class/thermal/thermal_zone0/temp) / 1000 ))C"

echo "=== USB 3.0 / fibra (SFP) — dependen del hub USB5744 ==="
lsusb | grep -q 5744 && echo "  USB5744 OK -> el SFP/LAN7801 se puede habilitar" \
  || echo "  USB5744 AUSENTE -> USB3.0 y SFP muertos (requiere la R de 10k / fix hardware)"
EOF

  c_info "RS-232: loopback = puentear J5 pin2-pin3 y probar ttyAMA4."
  c_info "RS-485: probar ttyAMA2 contra un 2do nodo (A=J5pin5,B=J5pin4,G=J5pin1)."
  c_ok "FASE 4 completa"
}

# =============================================================================
# MAIN
# =============================================================================
run_phase(){
  case "$1" in
    0) fase0_guia ;;
    1) fase1_drivers ;;
    2) fase2_config ;;
    3) fase3_flash_pws ;;
    4) fase4_puertos ;;
    5) fase5_flash_att ;;
    *) c_err "Fase desconocida: $1" ;;
  esac
}

menu(){
  cat <<M
=============================================================================
 provision-tpu-pws.sh — provisiona TPU (CM5) + PWS (ADIN6310) virgenes
=============================================================================
 Fases:
   0  Clon SO SD/SSD -> eMMC        (guia manual, consola del CM5)
   1  Drivers parcheados            (lan743x, adin1110/adin1100)   [SSH]
   2  config.txt + fan + EEPROM     (overlays uart/adin/lan, fan)  [SSH]
   3  Flasheo adin_phyfix al PWS    (OpenOCD + POR)                [local]
   4  Habilitar/verificar puertos   (eth/SPE/RS232/RS485/fan/usb)  [SSH]
   5  Flasheo de la ATT             (bootloader UART COM6)         [local]

 Uso:  ./provision-tpu-pws.sh 1 2 4      (fases sueltas)
       ./provision-tpu-pws.sh all        (1..4; la 0 y la 5 son aparte)
       ./provision-tpu-pws.sh 5          (solo la tarjeta ATT del Varec 2500)

 ANTES DE USAR: editar la seccion CONFIG (IP, CM5_MAC_ADIN unica, ART_DIR...).
=============================================================================
M
}

main(){
  [ $# -eq 0 ] && { menu; exit 0; }
  local args=("$@"); [ "${1:-}" = "all" ] && args=(1 2 3 4)
  for p in "${args[@]}"; do run_phase "$p"; done
  hr; c_ok "Listo. Fases ejecutadas: ${args[*]}"
}
main "$@"
