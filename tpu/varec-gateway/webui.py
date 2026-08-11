#!/usr/bin/env python3
"""UI web de la pasarela Varec: ver los tanques y configurar las salidas.

Editar YAML por SSH no es forma de operar un equipo de campo. Esta UI expone
lo mismo que el config, con validacion y red de seguridad.

Seguridad: autenticacion basica HTTP. OJO, la clave viaja en base64 (no
cifrado): es aceptable en una red industrial aislada, no en una expuesta a
internet. Para eso habria que poner un proxy con TLS delante.
"""
import base64
import copy
import hmac
import json
import os
import shutil
import threading
import secrets
import time

# --------------------------------------------------------------------- auth
# Sesiones en MEMORIA: un reinicio de la pasarela cierra todas. Es lo
# deseable -- tras tocar la configuracion conviene volver a identificarse.
_SESSIONS = {}                 # token -> epoch de caducidad
SESSION_TTL = 12 * 3600
COOKIE = "varec_sid"


def ui_user(cfg) -> str:
    return (os.environ.get("VAREC_UI_USER")
            or cfg.get("ui", {}).get("user") or "admin")


def ui_pass(cfg) -> str:
    return (os.environ.get("VAREC_UI_PASS")
            or cfg.get("ui", {}).get("password") or "")


def _purge():
    t = time.time()
    for k in [k for k, v in _SESSIONS.items() if v < t]:
        del _SESSIONS[k]


def check_login(user: str, pw: str, cfg) -> bool:
    """Valida usuario Y clave. compare_digest en ambos: sin cortocircuito,
    para no filtrar por tiempo cual de los dos fallo."""
    want_p = ui_pass(cfg)
    if not want_p:
        return True                       # sin clave configurada = abierto
    ok_u = hmac.compare_digest(user or "", ui_user(cfg))
    ok_p = hmac.compare_digest(pw or "", want_p)
    return ok_u and ok_p


def new_session() -> str:
    _purge()
    tok = secrets.token_urlsafe(32)
    _SESSIONS[tok] = time.time() + SESSION_TTL
    return tok


def drop_session(tok: str):
    _SESSIONS.pop(tok or "", None)


def session_of(headers) -> str:
    for part in (headers.get("Cookie") or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE:
            return v
    return ""


def check_auth(headers, cfg) -> bool:
    """True si la peticion trae sesion valida, o Basic con usuario y clave."""
    want = ui_pass(cfg)
    if not want:
        return True                       # sin clave configurada = abierto
    _purge()
    tok = session_of(headers)
    if tok and tok in _SESSIONS:
        return True
    # Basic se mantiene para scripts y curl
    got = headers.get("Authorization", "")
    if not got.startswith("Basic "):
        return False
    try:
        user, _, pw = base64.b64decode(got[6:]).decode().partition(":")
    except Exception:
        return False
    return check_login(user, pw, cfg)


LOGIN_PAGE = """<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__SITE__</title>
<style>
 *{box-sizing:border-box} body{margin:0;min-height:100vh;display:flex;
  align-items:center;justify-content:center;background:#0f1720;
  font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:#e6edf3}
 form{background:#161f2b;padding:32px;border-radius:12px;width:min(92vw,340px);
  border:1px solid #26313f;box-shadow:0 8px 32px #0006}
 h1{margin:0 0 4px;font-size:19px;text-align:center}
 p.sub{margin:0 0 22px;color:#8b98a5;font-size:13px;text-align:center}
 label{display:block;margin:14px 0 5px;font-size:13px;color:#8b98a5}
 input{width:100%;padding:10px 12px;border-radius:7px;border:1px solid #2b3846;
  background:#0f1720;color:#e6edf3;font-size:15px}
 input:focus{outline:none;border-color:#3d8bfd}
 button{width:100%;margin-top:22px;padding:11px;border:0;border-radius:7px;
  background:#3d8bfd;color:#fff;font-size:15px;font-weight:600;cursor:pointer}
 button:hover{background:#2f7ae8}
 .err{margin-top:16px;padding:9px 12px;border-radius:7px;background:#3b1d22;
  border:1px solid #6b2a33;color:#ffb4bd;font-size:13px}
 .calpt{border:1px solid #2b3846;border-radius:8px;padding:4px 12px 10px;margin:14px 0}
 .calpt h3{margin:10px 0 4px;font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:#8b98a5}
 .calin{display:flex;gap:8px;flex:1;align-items:center}
 .calin input{flex:1;min-width:0}
 .mini{margin-left:0;padding:6px 12px;font-size:13px;white-space:nowrap}
</style></head><body>
<form method="POST" action="/login">
  <h1>__SITE__</h1>
  <p class="sub">Monitoreo de niveles de tanque</p>
  <label for="u">Usuario</label>
  <input id="u" name="user" autocomplete="username" autofocus required>
  <label for="p">Clave</label>
  <input id="p" name="pass" type="password" autocomplete="current-password" required>
  <button type="submit">Entrar</button>
  __ERROR__
</form></body></html>"""


def login_page(error: str = "", name: str = "Pasarela Varec") -> bytes:
    msg = ('<div class="err">Usuario o clave incorrectos</div>' if error else '')
    return (LOGIN_PAGE.replace("__ERROR__", msg)
                      .replace("__SITE__", html_escape(name))).encode()


def html_escape(t: str) -> str:
    return (t.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


# ------------------------------------------------------------------ config
# Solo estas claves son editables desde la UI. Lo demas (rutas, ingesta) se
# toca por SSH a proposito: un error ahi deja el equipo sin recoger datos.
EDITABLE = {
    "site": ["name"],
    "mqtt": ["enabled", "host", "port", "username", "topic_prefix", "qos", "retain"],
    "modbus_tcp": ["enabled", "bind", "port", "unit_id"],
    "modbus_rtu": ["enabled", "device", "baud", "parity", "unit_id"],
    "http": ["enabled", "port"],
}

_VALID = {
    "qos": [0, 1, 2],
    "parity": ["N", "E", "O"],
    "baud": [9600, 19200, 38400, 115200],
}


def site_name(cfg: dict) -> str:
    """Como se llama ESTA pasarela. Con varias TPU desplegadas, un titulo
    generico no dice donde estas mirando."""
    return ((cfg.get("site", {}) or {}).get("name") or "").strip() or "Pasarela Varec"


def public_config(cfg: dict) -> dict:
    """El config que ve la UI. NUNCA incluye contraseñas."""
    out = {}
    for sec, keys in EDITABLE.items():
        s = cfg.get(sec, {}) or {}
        out[sec] = {k: s.get(k) for k in keys}
    return out


def validate(new: dict) -> list:
    """Devuelve la lista de errores. Validar ANTES de guardar es lo que evita
    dejar el equipo con un config que no arranca."""
    errs = []
    for sec, keys in EDITABLE.items():
        s = new.get(sec)
        if s is None:
            continue
        for k, v in s.items():
            if k not in keys:
                errs.append(f"{sec}.{k}: no editable")
                continue
            if k == "enabled" and not isinstance(v, bool):
                errs.append(f"{sec}.enabled debe ser true/false")
            if k in ("port", "unit_id", "baud", "qos") and not isinstance(v, int):
                errs.append(f"{sec}.{k} debe ser un numero")
            elif k == "port" and not (1 <= v <= 65535):
                errs.append(f"{sec}.port fuera de rango (1-65535)")
            elif k == "unit_id" and not (1 <= v <= 247):
                errs.append(f"{sec}.unit_id fuera del rango Modbus (1-247)")
            elif k in _VALID and v not in _VALID[k]:
                errs.append(f"{sec}.{k}: valor no soportado ({_VALID[k]})")

    # Dos servidores en el mismo puerto = uno de los dos no arranca.
    used = {}
    for sec in ("modbus_tcp", "http"):
        s = new.get(sec) or {}
        if s.get("enabled") and s.get("port"):
            p = s["port"]
            if p in used:
                errs.append(f"puerto {p} usado por {used[p]} y {sec}")
            used[p] = sec
    return errs


def save_config(cfg_path: str, cfg: dict, new: dict) -> tuple:
    """Guarda con backup. Devuelve (ok, mensaje)."""
    import yaml

    errs = validate(new)
    if errs:
        return False, "; ".join(errs)

    merged = copy.deepcopy(cfg)
    for sec, keys in EDITABLE.items():
        if sec not in new:
            continue
        merged.setdefault(sec, {})
        for k in keys:
            if k in new[sec]:
                merged[sec][k] = new[sec][k]

    # Red de seguridad: si el config nuevo dejara el servicio sin arrancar,
    # el backup permite volver atras sin ir fisicamente al equipo.
    try:
        if os.path.exists(cfg_path):
            shutil.copy2(cfg_path, cfg_path + ".bak")
        tmp = cfg_path + ".tmp"
        with open(tmp, "w") as f:
            yaml.safe_dump(merged, f, default_flow_style=False, sort_keys=False,
                           allow_unicode=True)
        os.replace(tmp, cfg_path)          # atomico: nunca un config a medias
    except Exception as e:
        return False, f"no se pudo guardar: {e}"
    return True, "guardado"


def restart_later(delay=1.0):
    """Sale del proceso para que systemd lo reinicie con el config nuevo.
    Se hace en diferido para poder responder al navegador primero."""
    def go():
        time.sleep(delay)
        os._exit(0)                        # Restart=always lo levanta
    threading.Thread(target=go, daemon=True).start()


# --------------------------------------------------------------------- HTML
PAGE = r"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pasarela Varec</title>
<style>
 :root{--bg:#f6f7f9;--fg:#1b1f23;--card:#fff;--line:#e1e4e8;--mut:#6a737d;
       --ok:#1a7f37;--bad:#cf222e;--acc:#0969da}
 @media(prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--card:#161b22;
       --line:#30363d;--mut:#8b949e;--ok:#3fb950;--bad:#f85149;--acc:#58a6ff}}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
 header{background:var(--card);border-bottom:1px solid var(--line);padding:14px 20px;
        display:flex;align-items:center;gap:14px;flex-wrap:wrap}
 h1{font-size:17px;margin:0;font-weight:600}
 .sp{flex:1}
 .tabs{display:flex;gap:4px}
 .tab{padding:7px 14px;border-radius:6px;cursor:pointer;border:1px solid transparent}
 .tab.on{background:var(--acc);color:#fff}
 main{max-width:1180px;margin:20px auto;padding:0 16px}
 .card{background:var(--card);border:1px solid var(--line);border-radius:8px;
       padding:16px;margin-bottom:14px}
 .card h2{font-size:14px;margin:0 0 12px;text-transform:uppercase;letter-spacing:.04em;
          color:var(--mut);display:flex;align-items:center;gap:8px}
 table{width:100%;border-collapse:collapse}
 th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);font-size:14px}
 th{color:var(--mut);font-weight:600;font-size:12px;text-transform:uppercase}
 /* Una tabla con muchas columnas se estrecha hasta partir CADA celda en dos
    lineas ("5 h 53" / "min", "en" / "linea") y la fila deja de leerse de un
    vistazo. Antes que eso, que la tabla se desborde y se desplace dentro de su
    propia caja: la fila se mantiene entera y el resto de la pagina no se
    descoloca. */
 .tw{overflow-x:auto}
 .tw table{min-width:1080px}
 /* Doce columnas no caben con el relleno normal: sobraban 49 px y aparecia una
    barra de desplazamiento. Tres pixeles menos por lado x 24 lados son 72 px,
    justo lo que hace falta. Preferible a quitar una columna o a encoger la
    letra, que es lo que de verdad se nota al leer. */
 .tw th,.tw td{padding-left:7px;padding-right:7px}
 /* Igual pero SIN ancho minimo: para tablas que caben casi siempre y solo
    necesitan la valvula de escape. Poner el min-width de .tw a la de switches,
    que tiene cinco columnas, le forzaria una barra de desplazamiento que no
    hace ninguna falta. */
 .tx{overflow-x:auto}
 .tx th{white-space:nowrap}
 /* Columnas que NUNCA deben partirse: son valores cortos donde el salto de
    linea solo estorba. El nombre y la MAC si pueden, que son los largos. */
 .nw{white-space:nowrap}
 .tw th{white-space:nowrap}
 .dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:6px}
 .up{background:var(--ok)} .down{background:var(--bad)}
 .row{display:grid;grid-template-columns:150px 1fr;gap:10px;align-items:center;
      margin-bottom:9px}
 /* ⚠️ Estas reglas SOLO estaban en la hoja de la pantalla de acceso
    (LOGIN_PAGE), no aqui. Sin display:flex el <span> era inline y el boton
    "Leer" se iba debajo del campo en vez de quedar al lado. */
 .calpt{border:1px solid var(--line);border-radius:8px;padding:4px 12px 10px;margin:14px 0}
 .calpt h3{margin:10px 0 4px;font-size:12px;letter-spacing:.06em;
      text-transform:uppercase;color:var(--mut)}
 .calin{display:flex;gap:8px;align-items:center}
 .calin input{flex:1;min-width:0}
 .mini{padding:7px 14px;font-size:13px;white-space:nowrap;flex:0 0 auto}
 label{color:var(--mut);font-size:13px}
 input[type=text],input[type=number],select{width:100%;padding:7px 9px;border-radius:6px;
      border:1px solid var(--line);background:var(--bg);color:var(--fg);font:inherit}
 input:disabled,select:disabled{opacity:.45}
 .sw{position:relative;width:42px;height:23px;flex:none}
 .sw input{opacity:0;width:0;height:0}
 .sl{position:absolute;inset:0;background:var(--line);border-radius:23px;cursor:pointer;
     transition:.2s}
 .sl:before{content:"";position:absolute;height:17px;width:17px;left:3px;bottom:3px;
     background:#fff;border-radius:50%;transition:.2s}
 .sw input:checked+.sl{background:var(--ok)}
 .sw input:checked+.sl:before{transform:translateX(19px)}
 button{background:var(--acc);color:#fff;border:0;padding:9px 18px;border-radius:6px;
        font:inherit;font-weight:600;cursor:pointer}
 button:disabled{opacity:.5;cursor:default}
 .msg{padding:10px 12px;border-radius:6px;margin-bottom:12px;font-size:14px;display:none}
 .msg.ok{background:#1a7f3722;color:var(--ok);display:block}
 .msg.err{background:#cf222e22;color:var(--bad);display:block}
 .mut{color:var(--mut);font-size:13px}
 .hd{display:flex;align-items:center;gap:10px;margin-bottom:12px}
 .hd h2{margin:0}
</style></head><body>
<header>
  <h1 id="site">Pasarela Varec</h1>
  <div class="tabs">
    <div class="tab on" data-t="dash">Tanques</div>
    <div class="tab" data-t="cfg">Settings</div>
  </div>
  <div class="sp"></div>
  <span class="mut" id="hdr">—</span>
  <a href="/logout" title="Cerrar sesión" style="margin-left:14px;padding:6px 12px;border:1px solid #2b3846;border-radius:7px;color:#8b98a5;text-decoration:none;font-size:13px">Cerrar sesión</a>
</header>
<main>
  <div id="dash">
    <div class="card">
      <h2>TPU <span class="mut" id="tpu_hdr" style="text-transform:none;font-weight:400"></span></h2>
      <div id="tpu_wrap" class="tx"><p class="mut">cargando…</p></div>
    </div>

    <!-- Placas conectadas sin numero de tanque. La tarjeta esta OCULTA cuando
         no hay ninguna: un hueco permanente que casi siempre dice "ninguna" se
         deja de mirar, y esto tiene que llamar la atencion el dia que aparece. -->
    <div class="card" id="nue_card" style="display:none;border-color:#d29922">
      <h2 style="color:#d29922">Placas nuevas sin asignar
        <span class="mut" id="nue_hdr" style="text-transform:none;font-weight:400"></span></h2>
      <p class="mut" style="margin:0 0 12px;font-size:13px">Estas placas est&aacute;n
        conectadas y transmitiendo, pero <b>no tienen n&uacute;mero de tanque</b>, as&iacute;
        que todav&iacute;a no se guarda su historial. Ponles el n&uacute;mero del tanque
        donde est&aacute;n instaladas y quedan de alta al momento: se escribe en su
        EEPROM y sobrevive a reinicios y a futuras actualizaciones de firmware.</p>
      <div class="tx" id="nue_wrap"></div>
    </div>

    <div class="card">
      <h2>Tanques</h2>
      <div class="tw">
      <table><thead><tr><th>ID</th><th>Nombre</th><th>MAC</th><th>Valor</th><th>Pulsos</th><th>Temp.</th><th>Errores</th><th>Envío</th>
        <th>Marcha</th>
        <th>Última conexión</th><th>Estado</th><th></th></tr></thead><tbody id="tb">
        <tr><td colspan="12" class="mut">cargando…</td></tr></tbody></table></div>
    </div>

    <div class="card">
      <h2>Consumo por slot <span class="mut" id="sw_hdr" style="text-transform:none;font-weight:400"></span></h2>
      <div id="sw_wrap"><p class="mut">cargando…</p></div>
      <p class="mut" style="margin:10px 0 0">Una seccion por equipo: el <b>power
        switch</b> y cada <b>field switch</b>, incluidos los encadenados por SPE
        colgando de otro field switch. Medido por el LTC4296 de cada uno.
        Un guion significa <b>sin lectura</b>, no cero: el ADC de puerto solo da dato
        valido en los puertos que estan entregando.</p>
      <p class="mut" style="margin:6px 0 0">La potencia es <b>estimada</b>: corriente
        medida por la tension de entrada, no por la de salida de cada puerto (el error
        es ~0,1&nbsp;%). ⚠️ Esa tensi&oacute;n se refresca al reintentar una
        clasificaci&oacute;n, y eso solo ocurre en los puertos que <b>no</b> entregan:
        con los cuatro slots cargados a la vez, el valor se congela en la
        &uacute;ltima medida.</p>
    </div>

    <div class="card">
      <p class="mut" style="margin:10px 0 0">El <b>tank_id</b> vive en la EEPROM de cada
        sensor y viaja en cada trama: al cambiarlo aquí se escribe <b>en el sensor</b> por
        Modbus, no en la pasarela. Así, si sustituyes una ATT averiada, le pones su id y
        listo.</p>
    </div>
  </div>

  <div id="cfg" style="display:none">
    <div class="msg" id="m"></div>
    <div id="secs"></div>
    <button id="save">Guardar y aplicar</button>
    <span class="mut" style="margin-left:10px">Al guardar, el servicio se reinicia
      (unos segundos).</span>
  </div>
</main>
<div id="calbg" style="display:none;position:fixed;inset:0;background:#000a;z-index:50;
     align-items:center;justify-content:center;padding:16px">
  <div class="card" style="width:min(94vw,620px);margin:0;max-height:92vh;overflow:auto">
    <h2 style="margin-top:0">Configurar <span id="caltit"></span></h2>
    <p class="mut" style="margin:0 0 12px;font-size:13px">Pulsos ahora:
      <b id="calvivo" style="color:#e6edf3;font-size:15px">—</b>
      <span style="font-size:12px">&nbsp;le&iacute;do del sensor en directo, no
      depende del periodo de env&iacute;o; espera a que se estabilice antes de
      capturar</span></p>

    <div style="display:flex;gap:8px;margin:0 0 12px">
      <button id="mg_geo" class="mini" style="flex:1">Por geometr&iacute;a</button>
      <button id="mg_2p"  class="mini" style="flex:1">Por dos puntos</button>
      <button id="mg_fw"  class="mini" style="flex:1">Firmware</button>
    </div>

    <!-- ===== firmware de ESTE sensor =====================================
         Aqui y no en una pestana global: en este modal ya se esta trabajando
         con un sensor concreto, asi que no hay que elegirlo de ninguna lista
         -- ni equivocarse de tanque al elegirlo. -->
    <div id="pan_fw" style="display:none">
      <div id="fw_img" class="mut" style="margin:0 0 10px">cargando&hellip;</div>
      <p style="margin:0 0 4px">
        <input type="file" id="fw_file" accept=".bin">
        <button id="fw_up" type="button" class="mini">Cargar imagen</button>
      </p>
      <p id="fw_msg" class="mut" style="margin:6px 0"></p>
      <button id="fw_go" type="button" style="width:100%;margin:6px 0 0">
        Actualizar este sensor</button>
      <p id="fw_est" class="mut" style="margin:8px 0 0"></p>
      <p class="mut" style="margin:10px 0 0;font-size:13px">Se sube el
        <b>zephyr.signed.bin</b> (la aplicaci&oacute;n firmada), <b>no</b> la
        imagen combinada del grabado por cable. Se comprueba la cabecera y el
        hash <b>antes</b> de guardarla: un fichero equivocado se rechaza aqu&iacute;
        y no llega a ninguna placa. La imagen vale para todos los sensores; se
        carga una vez y se actualiza desde el modal de cada uno.</p>
      <p class="mut" style="margin:6px 0 0;font-size:13px">La imagen nueva arranca
        <b>a prueba</b> y solo se confirma tras 4 env&iacute;os SPE correctos: si
        arrancara sin transmitir, MCUboot revierte sola a la anterior. Al terminar
        se relee la placa y se compara el hash, as&iacute; que solo dice
        <b>verificada</b> si lo demuestra.</p>
      <p class="mut" style="margin:6px 0 0;font-size:13px">⚠️ <b>MCUboot no se
        actualiza por aqu&iacute;</b>: vive en su propia partici&oacute;n. Cambiarlo
        exige cable, as&iacute; que la primera grabaci&oacute;n de cada sensor
        —en el banco, antes de instalarlo— tiene que llevar ya el bootloader bueno.</p>
    </div>

    <!-- ===== geometria: no hay que mover producto ======================= -->
    <div id="pan_geo">
      <p class="mut" style="margin:0 0 12px;font-size:13px">
        <b>nivel = altura de referencia &minus; pulsos &times; mm por pulso.</b>
        Los mm por pulso son una constante mec&aacute;nica del cabezal, y la
        altura de referencia se mide con cinta. <b>No hay que mover
        producto</b>, que es lo que hace viable dar de alta 50 tanques.</p>
      <div class="calpt">
        <h3>Geometr&iacute;a</h3>
        <div class="row"><label>mm por pulso</label>
          <span class="calin"><input type="number" step="0.000001" id="g_mm">
          <button id="g_ing" class="mini">Inglesa</button>
          <button id="g_met" class="mini">M&eacute;trica</button></span></div>
        <p class="mut" style="margin:2px 0 8px;font-size:12px">Cabezal
          <b>ingl&eacute;s</b> (dos ruedas contadoras, cuadrante en pulgadas):
          una vuelta = 1 ft = 304,8 mm &divide; 512 pulsos =
          <b>0,595312</b>. <b>M&eacute;trico</b> (tres ruedas): una vuelta =
          100 mm &divide; 512 = <b>0,195313</b>.</p>
        <div class="row"><label>Altura de referencia</label>
          <span class="calin"><input type="number" id="g_h" placeholder="mm">
          </span></div>
        <p class="mut" style="margin:2px 0 8px;font-size:12px">La cota que
          corresponde al <b>cero del contador</b>: el nivel que habr&iacute;a
          con 0 pulsos.</p>
        <div class="row"><label>Al subir los pulsos</label>
          <select id="g_s">
            <option value="-1">el nivel BAJA (flotador y cinta)</option>
            <option value="1">el nivel SUBE</option>
          </select></div>
      </div>

      <!-- El acople del encoder al cabezal es nuestro, no de Varec: los mm por
           pulso de arriba son el valor TEORICO. Esto lo confirma en campo
           contra el propio contador mec&aacute;nico del medidor. -->
      <div class="calpt">
        <h3>Medir los mm por pulso (opcional)</h3>
        <p class="mut" style="margin:2px 0 8px;font-size:13px">Mueve la cinta
          con la perilla de comprobaci&oacute;n y compara contra el contador
          mec&aacute;nico del cabezal. Cuanto m&aacute;s la muevas, mejor.</p>
        <div class="row"><label>Pulsos antes</label>
          <span class="calin"><input type="number" id="m_c1">
          <button id="m_n1" class="mini">Leer</button></span></div>
        <div class="row"><label>Pulsos despu&eacute;s</label>
          <span class="calin"><input type="number" id="m_c2">
          <button id="m_n2" class="mini">Leer</button></span></div>
        <div class="row"><label>Se movi&oacute;</label>
          <span class="calin"><input type="number" id="m_mm" placeholder="mm">
          <button id="m_go" class="mini">Calcular</button></span></div>
        <p id="m_out" class="mut" style="margin:8px 0 0;font-size:13px"></p>
      </div>
      <p id="gcalc" class="mut" style="margin:10px 0"></p>
    </div>

    <!-- ===== dos puntos: lo exacto, si se puede mover el flotador ======= -->
    <div id="pan_2p" style="display:none">
      <p class="mut" style="margin:0 0 12px;font-size:13px">Dos niveles reales
        medidos. Es lo exacto, pero hay que <b>mover el flotador</b> entre
        ellos. <b>Sep&aacute;ralos todo lo que puedas.</b> Un tercer punto sirve
        para <i>comprobar</i>, no para afinar: si se desv&iacute;a, revisa la
        mec&aacute;nica.</p>
      <div class="calpt">
        <h3>Punto A</h3>
        <div class="row"><label>Pulsos</label>
          <span class="calin"><input type="number" id="ca_c">
          <button id="ca_now" class="mini">Leer</button></span></div>
        <div class="row"><label>Nivel real</label>
          <span class="calin"><input type="number" id="ca_l" placeholder="mm"></span></div>
      </div>
      <div class="calpt">
        <h3>Punto B</h3>
        <div class="row"><label>Pulsos</label>
          <span class="calin"><input type="number" id="cb_c">
          <button id="cb_now" class="mini">Leer</button></span></div>
        <div class="row"><label>Nivel real</label>
          <span class="calin"><input type="number" id="cb_l" placeholder="mm"></span></div>
      </div>
      <p id="calc" class="mut" style="margin:10px 0"></p>
    </div>

    <div class="row"><label>Unidad</label><input type="text" id="c_u" value="mm"></div>

    <!-- ⚠️ VA DENTRO DEL DIALOGO, no en la fila de la tabla: la tabla se
         redibuja con CADA trama (~1 s) y cualquier control abierto en una fila
         se destruia al instante. El dialogo esta a salvo porque el refresco se
         salta mientras esta abierto. -->
    <div class="calpt">
      <h3>Diagn&oacute;stico</h3>
      <p class="mut" style="margin:2px 0 8px;font-size:13px">Los LEDs de canal del
        encoder parpadean con cada pulso. Se encienden para <b>comprobar el
        cableado</b> estando junto al tanque. <b>No se guardan</b>: un reinicio
        del sensor los deja apagados.</p>
      <div style="display:flex;gap:8px">
        <button id="led_on"  class="mini" style="flex:1;background:#2b3846">Encender LEDs</button>
        <button id="led_off" class="mini" style="flex:1;background:#2b3846">Apagar LEDs</button>
      </div>
      <p id="ledm" class="mut" style="margin:8px 0 0;font-size:13px"></p>
    </div>
    <div style="display:flex;gap:8px;margin-top:6px">
      <button id="calsave" style="flex:1">Guardar</button>
      <button id="calclose" style="flex:0 0 auto;background:#2b3846">Cancelar</button>
    </div>
    <p id="calm" class="mut" style="margin:10px 0 0"></p>
  </div>
</div>
<script>
const SECS = {
  site:       {t:"Identificación", nosw:1,
               f:[["name","Nombre de esta TPU","text"]]},
  mqtt:       {t:"MQTT", f:[["host","Broker","text"],["port","Puerto","number"],
                            ["username","Usuario","text"],["topic_prefix","Prefijo","text"],
                            ["qos","QoS","sel",[0,1,2]],["retain","Retain","bool"]]},
  modbus_tcp: {t:"Modbus TCP", f:[["bind","Escuchar en","text"],["port","Puerto","number"],
                            ["unit_id","Unit ID","number"]]},
  modbus_rtu: {t:"Modbus RTU", f:[["device","Puerto serie","text"],
                            ["baud","Baudios","sel",[9600,19200,38400,115200]],
                            ["parity","Paridad","sel",["N","E","O"]],
                            ["unit_id","Unit ID","number"]]},
  http:       {t:"HTTP / API", f:[["port","Puerto","number"]]},
};
let CFG={};

const $=s=>document.querySelector(s);
// Un panel por pestana, por data-t. Con una lista, anadir otra pestana es
// una linea; con un if por panel, olvidar uno deja dos visibles a la vez.
const PANELES = ['dash', 'cfg'];
document.querySelectorAll('.tab').forEach(x=>x.onclick=()=>{
  document.querySelectorAll('.tab').forEach(y=>y.classList.toggle('on',y===x));
  PANELES.forEach(p => $('#'+p).style.display = (x.dataset.t==p ? '' : 'none'));
});

let editing=false;

async function saveId(btn){
  const tr=btn.closest('tr'), inp=tr.querySelector('.tid');
  const old=btn.dataset.id, nid=parseInt(inp.value,10);
  if(!confirm(`¿Cambiar el tank_id ${old} → ${nid}?

Se escribe en la EEPROM del `+
              `sensor por Modbus y se aplica de inmediato.`)) return;
  btn.disabled=true; btn.textContent='…';
  try{
    const r=await fetch(`/api/tank/${old}/config`,{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({tank_id:nid})});
    const d=await r.json();
    if(r.ok){ btn.textContent='✓'; setTimeout(tanks,1500); }
    else { alert('No se pudo: '+(d.error||'error')); btn.textContent='Guardar';
           btn.disabled=false; }
  }catch(e){ alert('Error: '+e); btn.textContent='Guardar'; btn.disabled=false; }
}

// Marca de tiempo legible. Un sensor caido necesita CUANDO fue la ultima
// conexion, no cuantos segundos han pasado: "hace 86400s" no ayuda a nadie.
function fechaHora(ts){
  if(!ts) return '—';
  const d = new Date(ts*1000);
  const p = n => String(n).padStart(2,'0');
  return `${p(d.getDate())}/${p(d.getMonth()+1)}/${d.getFullYear()} `
       + `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
function hace(s){
  if(s==null) return '';
  if(s < 60)    return s+' s';
  if(s < 3600)  return Math.floor(s/60)+' min';
  if(s < 86400) return Math.floor(s/3600)+' h';
  return Math.floor(s/86400)+' d';
}
// Marcha con DOS unidades. hace() vale para "hace 3 h", donde la precision
// sobra, pero un uptime de "3 d" esconde si el equipo lleva tres dias o casi
// cuatro -- y con 50 tanques lo que se busca en esta columna es justo el que
// se reinicio anoche.
function marcha(s){
  if(s==null) return '<span class="mut">—</span>';
  const d=Math.floor(s/86400), h=Math.floor(s%86400/3600);
  const m=Math.floor(s%3600/60);
  if(d) return d+' d '+h+' h';
  if(h) return h+' h '+m+' min';
  if(m) return m+' min';
  return s+' s';
}
// Un equipo que lleva menos de 5 min en marcha acaba de reiniciarse. Se marca
// en ambar porque en una lista de 50 tanques eso es lo unico que distingue
// "lleva semanas funcionando" de "se cayo mientras no mirabas".
const RECIEN = 300;
function marcaMarcha(s){
  if(s==null) return '<span class="mut">—</span>';
  return (s < RECIEN)
    ? '<span style="color:#d29922" title="reiniciado hace poco">'+marcha(s)+'</span>'
    : marcha(s);
}

// Un Varec mide nivel de liquido: se mueve en minutos, no en
// milisegundos. Por debajo de 1 s no se gana dato, solo se gasta enlace.
const PERIODOS = [[1000,"1 s"],[2000,"2 s"],[5000,"5 s"],[10000,"10 s"],
                  [15000,"15 s"],[30000,"30 s"],[60000,"1 min"]];

// La ATT no lleva sensor de humedad: el firmware manda el centinela y la
// pasarela lo guarda como null. Se reserva la MISMA celda para cuando lo
// haya, en vez de anadir ahora una columna siempre vacia.
function ambiente(t){
  if(t.temp_c==null) return '<span class="mut">—</span>';
  const h = (t.humi_rh!=null) ? ` <span class="mut">/ ${t.humi_rh.toFixed(0)} %</span>` : '';
  return `${t.temp_c.toFixed(1)} <span class="mut">°C</span>${h}`;
}

// ★ Periodo CONFIRMADO por el sensor, por tanque, tras un cambio.
//
// El desplegable se pintaba con el periodo OBSERVADO entre tramas, y eso
// tarda DOS tramas en reflejar un cambio -- con 30 s, hasta un minuto. Como el
// panel se refresca cada 5 s, la seleccion del operador desaparecia a los
// pocos segundos y parecia que la orden se habia perdido. No se perdia: la
// realimentacion iba por detras.
//
// Ahora manda lo que el sensor CONFIRMA tener, y el observado solo se muestra
// al lado hasta que los dos coinciden.
const PMS_FIJADO = {};

function selPeriodo(t){
  const obs = t.period_s;
  const fij = PMS_FIJADO[t.tank_id];
  // Cuando el observado alcanza a lo configurado, se suelta la fijacion y el
  // panel vuelve a reflejar la realidad medida.
  if(fij && obs && Math.abs(fij/1000-obs) < 0.6){ delete PMS_FIJADO[t.tank_id]; }
  const marca = PMS_FIJADO[t.tank_id] || (obs ? obs*1000 : null);
  const sel = PERIODOS.map(([ms,lab])=>
      `<option value="${ms}" ${marca && Math.abs(ms-marca)<600?'selected':''}>${lab}</option>`
    ).join('');
  // Mientras no coincidan, se dice lo que se ve: es informacion, no un fallo.
  const espera = PMS_FIJADO[t.tank_id]
    ? ` <span class="mut" title="el periodo medido tarda dos tramas en ponerse al dia">observado ${obs || '—'} s</span>`
    : '';
  return `<select class="pms" data-id="${t.tank_id}"
            ${t.online?'':'disabled title="el sensor no responde"'}>
      <option value="">—</option>${sel}</select>${espera}`;
}

async function setPeriodo(sel){
  const id = sel.dataset.id, ms = sel.value;
  if(!ms) return;
  sel.disabled = true;
  try{
    const r = await fetch(`/api/tank/${id}/config`,{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({push_ms:parseInt(ms,10)})});
    const d = await r.json();
    if(!r.ok){ alert('No se pudo cambiar: '+(d.error||'error')); }
    // Lo que el SENSOR confirma tener. Se fija hasta que el periodo observado
    // lo alcance, para que el refresco no repinte el valor viejo encima.
    else if(d.push_ms){ PMS_FIJADO[id] = d.push_ms; tanks(); }
  }catch(e){ alert('No se pudo cambiar: '+e); }
  sel.disabled = false;
}

async function tanks(){
  // No repintar mientras se edita: borraria lo que el usuario esta escribiendo.
  if(document.querySelector('.tid:focus, .pms:focus')) return;
  if($c('calbg') && $c('calbg').style.display==='flex') return;
  try{
    // Consumo del PSE. En su propio try: si falla, la tabla de tanques -- que
    // es lo importante -- se sigue pintando igual.
    try{
      const rs=await fetch('/api/switches'); const sw=await rs.json();
      const eq=sw.switches||[];
      $('#sw_hdr').textContent = eq.length ? (eq.length+' equipo'+(eq.length==1?'':'s')) : '';
      const porClave = {}; eq.forEach(e=>porClave[e.clave]=e);

      // Tabla de slots de un equipo, con lo que cuelga de cada uno.
      // El rotulo lo da la pasarela: en el field switch los numeros de
      // potencia y de datos NO coinciden con la serigrafia (MAPA-SLOTS.md).
      const filas = s => {
        const ps = s.puertos||[];
        if(!ps.length) return '<tr><td colspan="5" class="mut">sin puertos</td></tr>';
        return ps.map(x=>{
          const hijos = (s.hijos_por_puerto||{})[x.rotulo]||[];
          const cuelga = hijos.length ? hijos.map(h=> h.tipo==='tanque'
              ? ('tanque '+h.id)
              : ((porClave[h.id]?(porClave[h.id].tipo==='mps'?'power switch':'field switch'):'equipo')
                 +' '+String(h.id).split(':').pop().slice(0,8))).join(', ')
            : '<span class="mut">—</span>';
          return `<tr>
            <td>${x.rotulo||x.slot}</td>
            <td><b>${(s.vivo && x.ma!=null) ? x.ma+' mA' : '—'}</b></td>
            <td><b>${(s.vivo && x.w!=null) ? x.w.toFixed(2)+' W' : '—'}</b></td>
            <td class="mut">${x.estado}</td>
            <td>${cuelga}</td></tr>`;
        }).join('');
      };

      // Render recursivo: cada equipo y, anidados debajo, los que cuelgan de el.
      const nodo = (s, nivel) => {
        const nom = s.tipo==='mps' ? 'Power switch' : 'Field switch';
        const ident = s.id_estable
          ? `id ${s.dev_id.toString(16).padStart(16,'0')}`
          : `<span title="firmware antiguo: la MAC cambia en cada arranque">MAC ${s.mac} ⚠️</span>`;
        const est = s.vivo ? '' : (s.edad_s==null ? ' — sin telemetria' : ` — sin datos hace ${s.edad_s}s`);
        const tot = (s.vivo && s.w_total!=null)
          ? `<tr><td class="mut">total</td><td></td><td><b>${s.w_total.toFixed(2)} W</b></td>
             <td class="mut" colspan="2">a ${(s.w_vin_mv/1000).toFixed(1)} V</td></tr>` : '';
        const hijosSw = [];
        Object.values(s.hijos_por_puerto||{}).forEach(l=>l.forEach(h=>{
          if(h.tipo==='switch' && porClave[h.id]) hijosSw.push(porClave[h.id]);
        }));
        return `<div style="margin:0 0 18px 0;${nivel?'padding-left:18px;border-left:2px solid var(--line)':''}">
          <h3 style="margin:0 0 6px;font-size:14px">${nom}
            <span class="mut" style="font-weight:400">${ident}${est}</span></h3>
          <table><thead><tr><th>Slot</th><th>Corriente</th><th>Potencia</th>
            <th>Estado</th><th>Conectado</th></tr></thead>
            <tbody>${filas(s)}${tot}</tbody></table>
        </div>` + hijosSw.map(h=>nodo(h, nivel+1)).join('');
      };

      const raices = eq.filter(e=>!e.padre);
      $('#sw_wrap').innerHTML = eq.length
        ? (raices.length ? raices : eq).map(e=>nodo(e,0)).join('')
        : '<p class="mut">sin telemetria de ningun switch</p>';
    }catch(e){ /* el panel de tanques manda: no romper por esto */ }

    const r=await fetch('/api/tanks'); const d=await r.json();
    $('#hdr').textContent = d.n+' tanque'+(d.n==1?'':'s');
    $('#tb').innerHTML = d.tanks.length ? d.tanks.map(t=>`<tr>
      <td><input type="number" class="tid" value="${t.tank_id}" data-old="${t.tank_id}"
           style="width:80px"></td>
      <td>${t.name||''}</td>
      <td class="mut" style="font-family:ui-monospace,monospace;font-size:12px">${t.mac||'—'}</td>
      <td class="nw"><b>${t.value!=null? t.value.toFixed(2) : '—'}</b> <span class="mut">${t.unit||''}</span></td>
      <td class="nw">${t.count??0}</td>
      <td class="nw">${ambiente(t)}</td>
      <td>${t.errors??0}</td>
      <td>${selPeriodo(t)}</td>
      <td class="nw">${t.online ? marcaMarcha(t.uptime_s) : '<span class="mut">—</span>'}</td>
      <td class="nw">${fechaHora(t.ts)}<br><span class="mut" style="font-size:11px">${t.age_s!=null? 'hace '+hace(t.age_s) : ''}</span></td>
      <td class="nw"><span class="dot ${t.online?'up':'down'}"></span>${t.online?'en línea':'sin señal'}</td>
      <td style="white-space:nowrap"><button class="sid" data-id="${t.tank_id}"
           style="padding:5px 10px;font-size:13px" disabled>Guardar</button>
        <button class="cal" data-id="${t.tank_id}"
           style="padding:5px 10px;font-size:13px;background:#2b3846">Config</button></td>
      </tr>`).join('') :
      '<tr><td colspan="12" class="mut">ningún tanque dado de alta todavía</td></tr>';
    // El boton solo se activa si el valor cambio: evita escrituras accidentales
    // a la EEPROM del sensor.
    document.querySelectorAll('.tid').forEach(x=>x.oninput=()=>{
      const b=x.closest('tr').querySelector('.sid');
      b.disabled = (x.value==x.dataset.old || !x.value);
    });
    document.querySelectorAll('.sid').forEach(b=>b.onclick=()=>saveId(b));
    CALT = d.tanks;
    document.querySelectorAll('.cal').forEach(b=>b.onclick=()=>calOpen(+b.dataset.id));
    document.querySelectorAll('.pms').forEach(x=>x.onchange=()=>setPeriodo(x));
    pintaNuevas(d.nuevas||[]);
  }catch(e){ $('#hdr').textContent='sin conexión'; }
}

// ---- placas nuevas sin asignar --------------------------------------
// Salen de la MISMA respuesta de /api/tanks que ya se pide cada 5 s, asi que
// una placa recien conectada aparece sola sin pedir nada nuevo.
// ⚠️ NO se redibuja la tabla en cada refresco. Es la misma trampa que obligo a
// sacar la calibracion a un dialogo: el panel se refresca cada 5 s, y volver a
// escribir el innerHTML DESTRUYE el <input> -- con el numero de tanque que el
// operario estuviera tecleando dentro. Mientras las placas sean las mismas se
// actualizan SOLO las celdas volatiles, celda a celda, y no se toca el campo.
let NUE_CLAVE = '';
function pintaNuevas(ns){
  const card = $('#nue_card');
  if(!card) return;
  if(!ns.length){ card.style.display='none'; NUE_CLAVE=''; return; }
  card.style.display='';
  $('#nue_hdr').textContent = ns.length+' placa'+(ns.length==1?'':'s')+' esperando número';

  const celdas = n => ({
    c: String(n.count??0),
    t: (n.temp_c!=null ? n.temp_c.toFixed(1)+' °C' : '—'),
    m: marcaMarcha(n.uptime_s),
    v: (n.online ? '<span class="dot up"></span>ahora'
                 : '<span class="dot down"></span>hace '+hace(n.age_s)),
  });

  const clave = ns.map(n=>n.mac).join(',');
  if(clave === NUE_CLAVE){
    ns.forEach(n=>{
      const c = celdas(n);
      for(const k in c){
        const e = document.getElementById('nue_'+k+'_'+n.mac);
        if(e && e.innerHTML !== c[k]) e.innerHTML = c[k];
      }
    });
    return;                 // el <input> y su contenido quedan intactos
  }

  // Cambio la lista de placas: toca redibujar. Se conserva lo ya tecleado, que
  // si no se perderia al aparecer o marcharse OTRA placa distinta.
  const escrito = {};
  document.querySelectorAll('.nid').forEach(i=>{ if(i.value) escrito[i.dataset.mac]=i.value; });
  NUE_CLAVE = clave;
  $('#nue_wrap').innerHTML = `<table><thead><tr>
      <th>MAC</th><th>Pulsos</th><th>Temp.</th><th>Marcha</th><th>Visto</th>
      <th>N&uacute;mero de tanque</th></tr></thead><tbody>`
    + ns.map(n=>{ const c = celdas(n); return `<tr>
      <td class="nw" style="font-family:ui-monospace,monospace;font-size:12px">${n.mac}</td>
      <td class="nw" id="nue_c_${n.mac}">${c.c}</td>
      <td class="nw" id="nue_t_${n.mac}">${c.t}</td>
      <td class="nw" id="nue_m_${n.mac}">${c.m}</td>
      <td class="nw" id="nue_v_${n.mac}">${c.v}</td>
      <td class="nw"><span class="calin">
        <input type="number" class="nid" data-mac="${n.mac}" min="1" max="65535"
               placeholder="p. ej. 7" style="width:110px"
               value="${escrito[n.mac]||''}">
        <button class="nasig mini" data-mac="${n.mac}">Asignar</button></span></td>
    </tr>`; }).join('')
    + `</tbody></table><p id="nue_msg" class="mut" style="margin:10px 0 0"></p>`;
  document.querySelectorAll('.nasig').forEach(b=>b.onclick=()=>asignarNueva(b));
}

async function asignarNueva(btn){
  const mac = btn.dataset.mac;
  const inp = document.querySelector('.nid[data-mac="'+mac+'"]');
  const msg = $('#nue_msg');
  const id = parseInt(inp.value, 10);
  if(!(id>=1 && id<=65535)){ msg.textContent='Escribe un número de tanque entre 1 y 65535.'; return; }
  // Se desactiva mientras va: escribir dos veces la EEPROM por un doble clic
  // no rompe nada, pero deja dos peticiones Modbus compitiendo por la placa.
  btn.disabled = true; msg.textContent = 'Escribiendo en la placa...';
  try{
    const r = await fetch('/api/nueva/'+mac+'/asignar', {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({tank_id:id})});
    const d = await r.json();
    if(r.ok){ msg.innerHTML = '<span style="color:#3fb950">Placa '+mac+' asignada al tanque '
        + d.tank_id+' ('+d.ip+'). Aparecerá en la tabla de tanques enseguida.</span>';
      tanks();
    } else { msg.innerHTML = '<span style="color:#ffb4bd">'+(d.error||'error')+'</span>';
             btn.disabled = false; }
  }catch(e){ msg.textContent = 'No se pudo asignar: '+e; btn.disabled = false; }
}

// Acciones sobre un sensor. Son EXPLICITAS ("apagar"/"encender") y no un
// interruptor de estado: la pasarela NO conoce hoy el valor de esas banderas
// -- no viajan en la trama de telemetria -- y un interruptor tendria que
// adivinar la posicion, que es peor que no mostrarla.
async function accion(v){
  const id = CALID;
  if(!id) return;
  // El RS-485 se quito del panel a proposito: apagarlo deja al sensor sin
  // responder por Modbus RTU, y cada accion cuesta una conexion TCP contra una
  // placa con fuga de buferes conocida. La capacidad sigue en la API
  // (/api/tank/<id>/config con {"rs485":false}) para quien sepa lo que hace.
  const body = {'ledson':{enc_leds:true}, 'ledsoff':{enc_leds:false}}[v];
  const txt  = {'ledson':'encender los LEDs del encoder',
                'ledsoff':'apagar los LEDs del encoder'}[v];
  try{
    const r = await fetch('/api/tank/'+id+'/config',{method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    const d = await r.json();
    const m = $c('ledm');
    if(m){ m.textContent = r.ok ? ('Hecho: '+txt)
                                : ('No se pudo '+txt+': '+(d.error||r.status)); }
  }catch(e){ const m=$c('ledm'); if(m){ m.textContent='No se pudo '+txt+': '+e; } }
}

let CALT = [], CALID = null;
const $c = id => document.getElementById(id);

// mm de cinta por pulso del encoder, segun la escala del cuadrante del Varec
// 2500 (manual IOM001 rev I, p. 56-57) y los 512 pulsos por vuelta del
// encoder en cuadratura x4. El manual NO publica el perimetro del pinon, pero
// la escala del cuadrante cuelga del mismo eje y es equivalente.
const MMP_INGLESA = 304.8/512;   // 1 ft por vuelta
const MMP_METRICA = 100.0/512;   // 100 mm por vuelta

// Que formulario esta a la vista. Por defecto geometria: es el unico que no
// obliga a mover producto, y con 50 tanques es el que se va a usar siempre.
let CALMODO = 'geo';
function calModo(m){
  CALMODO = m;
  const modos = {geo:'pan_geo', '2p':'pan_2p', fw:'pan_fw'};
  const bots  = {geo:'mg_geo',  '2p':'mg_2p',  fw:'mg_fw'};
  for(const k in modos){
    $c(modos[k]).style.display   = (k===m) ? '' : 'none';
    $c(bots[k]).style.background = (k===m) ? '' : '#2b3846';
  }
  // Guardar/Cancelar son de la calibracion: en firmware no pintan nada, y un
  // "Guardar" ahi invita a creer que confirma la actualizacion.
  $c('calsave').style.display = (m==='fw') ? 'none' : '';
  if(m === 'fw'){ fwPinta(); } else { calCalc(); }
}

function geoCalc(){
  const mm=+$c('g_mm').value, h=+$c('g_h').value, s=+$c('g_s').value;
  const el=$c('gcalc');
  if($c('g_mm').value==='' || $c('g_h').value===''){
    el.textContent='Introduce los mm por pulso y la altura de referencia.'; return null; }
  if(!(mm>0)){ el.innerHTML='<span style="color:#ffb4bd">Los mm por pulso deben ser '
    + 'mayores que cero. El sentido se elige abajo.</span>'; return null; }
  const scale=s*mm, offset=h, u=$c('c_u').value;
  el.innerHTML = 'escala <b>'+scale.toFixed(6)+'</b> '+u+'/pulso &middot; '
    + 'offset <b>'+offset.toFixed(2)+'</b><br>'
    + 'con 0 pulsos el nivel ser&aacute; <b>'+offset.toFixed(1)+' '+u+'</b>'
    + ((h<300||h>30000) ? '<br><span style="color:#ffb4bd">Fuera del alcance de un '
      + '2500 (0,3 a 27,4 m). &iquest;Est&aacute; en mil&iacute;metros?</span>' : '');
  return {mm_por_pulso:mm, altura_referencia:h, sentido:s};
}

// Medida de campo de los mm por pulso, contra el contador mecanico del propio
// cabezal. El acople del encoder es nuestro, no de Varec: el valor teorico es
// un punto de partida, esto es la comprobacion.
function geoMedir(){
  const c1=+$c('m_c1').value, c2=+$c('m_c2').value, mm=+$c('m_mm').value;
  const el=$c('m_out');
  if(['m_c1','m_c2','m_mm'].some(k=>$c(k).value==='')){
    el.textContent='Faltan las dos lecturas de pulsos y el desplazamiento.'; return; }
  const dp=c2-c1;
  if(dp===0){ el.innerHTML='<span style="color:#ffb4bd">Los pulsos no cambiaron.</span>'; return; }
  const v=Math.abs(mm/dp);
  $c('g_mm').value = v.toFixed(6);
  // El signo tambien sale de la medida: si al aumentar los pulsos el contador
  // marco menos nivel, el sentido es -1. Se deduce, no se pregunta otra vez.
  const s = (dp>0 && mm<0) || (dp<0 && mm>0) ? -1 : 1;
  $c('g_s').value = String(s);
  const teo = Math.abs(v-MMP_INGLESA)<Math.abs(v-MMP_METRICA) ? MMP_INGLESA : MMP_METRICA;
  const desv = 100*Math.abs(v-teo)/teo;
  el.innerHTML = '<b>'+v.toFixed(6)+'</b> mm/pulso en '+Math.abs(dp)+' pulsos &middot; '
    + 'sentido '+(s<0?'baja':'sube')+'<br>'
    + (desv<5 ? 'Coincide con el te&oacute;rico ('+teo.toFixed(6)+', '+desv.toFixed(1)+'%).'
              : '<span style="color:#ffb4bd">Se aparta un '+desv.toFixed(1)+'% del te&oacute;rico '
                + 'm&aacute;s cercano ('+teo.toFixed(6)+'): el acople del encoder lleva '
                + 'reducci&oacute;n, o el desplazamiento se ley&oacute; mal.</span>');
  geoCalc();
}

function calCalc(){
  if(CALMODO==='geo') return geoCalc();
  const ac=+$c('ca_c').value, al=+$c('ca_l').value;
  const bc=+$c('cb_c').value, bl=+$c('cb_l').value;
  const el=$c('calc');
  if(['ca_c','ca_l','cb_c','cb_l'].some(k=>$c(k).value==='')){
    el.textContent='Introduce los dos puntos.'; return null; }
  if(ac===bc){ el.innerHTML='<span style="color:#ffb4bd">Los dos puntos tienen los mismos '
    + 'pulsos: no definen una recta.</span>'; return null; }
  const scale=(bl-al)/(bc-ac), offset=al-scale*ac, sep=Math.abs(bc-ac);
  el.innerHTML = 'escala <b>'+scale.toFixed(6)+'</b> '+$c('c_u').value+'/pulso &middot; '
    + 'offset <b>'+offset.toFixed(2)+'</b>'
    + (sep<50 ? '<br><span style="color:#ffb4bd">Puntos muy pr&oacute;ximos: la pendiente '
      + 'saldr&aacute; imprecisa</span>' : '');
  return {scale:scale, offset:offset};
}
function calOpen(id){
  CALID = id;
  const t = CALT.find(x=>x.tank_id===id) || {};
  $c('caltit').textContent = 'tanque '+id + (t.name? ' - '+t.name : '');
  ['ca_c','ca_l','cb_c','cb_l','m_c1','m_c2','m_mm'].forEach(k=>$c(k).value='');
  $c('m_out').textContent='';
  $c('c_u').value = t.unit || 'mm';
  // Se rellena con la calibracion VIGENTE leida como geometria, no con un
  // formulario en blanco: lo normal al reabrir es retocar la altura, y volver
  // a teclear los mm por pulso solo invita a equivocarse. Sin calibrar, el
  // teorico del cabezal ingles, que es el que hay instalado.
  const cal = (t.scale!=null && +t.scale!==1);
  $c('g_mm').value = cal ? Math.abs(+t.scale).toFixed(6) : MMP_INGLESA.toFixed(6);
  $c('g_h').value  = cal ? (+t.offset) : '';
  $c('g_s').value  = (cal && +t.scale>0) ? '1' : '-1';
  $c('calm').textContent = (t.scale!=null)
    ? 'Actual: escala '+(+t.scale).toFixed(6)+' / offset '+(+t.offset).toFixed(2)
    : 'Sin calibrar: el valor mostrado son los pulsos crudos.';
  calModo('geo');
  $c('calbg').style.display='flex';
  calVivoArrancar();
}
async function calNow(campo){
  // ⚠️ SE PIDE EL DATO AHORA, no se usa la foto en cache. Mientras el modal
  // esta abierto el refresco general esta PARADO a proposito (para que la
  // tabla no se redibuje bajo un control abierto), asi que CALT se queda
  // congelado en el instante en que se abrio. Leyendo de ahi, "Leer" devolvia
  // siempre la misma cifra por mucho que la cinta se moviera -- y el punto B
  // salia identico al A, que es justo lo que invalida la recta.
  //
  // ★ Y se lee DEL SENSOR, no del ultimo valor empujado: capturar un punto de
  // calibracion con una cifra de hace 30 s falsea la recta sin avisar. Si no
  // contesta se cae al empujado, pero diciendolo.
  try{
    const r = await fetch(`/api/tank/${CALID}/vivo`);
    if(r.ok){
      const d = await r.json();
      $c(campo).value = d.count; $c('calm').textContent=''; calCalc();
      return;
    }
  }catch(e){}
  try{
    const d = await (await fetch('/api/tanks')).json();
    const t = (d.tanks||[]).find(x=>x.tank_id===CALID);
    if(!t || t.count==null){ $c('calm').textContent='Ese sensor no reporta pulsos ahora.'; return; }
    $c(campo).value = t.count; calCalc();
    $c('calm').textContent='Ojo: el sensor no responde a la lectura directa; '
      +'este valor es el ultimo recibido y puede tener hasta un periodo de retraso.';
  }catch(e){ $c('calm').textContent='No pude leer los pulsos: '+e; }
}

// Lectura EN VIVO dentro del modal. El refresco general esta parado, asi que
// este es el unico sitio donde se ve moverse la cinta -- y hace falta para
// saber cuando se ha estabilizado antes de capturar un punto.
let CALVIVO = null;
// Declarado junto al otro temporizador del modal, y no en el bloque de mas
// abajo donde se usa: calVivoParar() lo limpia y esta definida ANTES. Hoy
// funcionaria igual --el cuerpo corre despues de cargar el script-- pero con
// `let` eso es zona muerta esperando a que alguien adelante una llamada.
let FWTIMER = null;
// ★ El conteo se le PREGUNTA al sensor, no se espera a que lo empuje.
//
// Antes se leia de /api/tanks, o sea el ultimo valor empujado: el refresco lo
// marcaba el periodo de envio. Con 30 s, quien mueve la cinta con la perilla
// esperaba medio minuto por cada lectura. Preguntando por Modbus se ve el
// encoder moverse cuando se mueve, y el almacenamiento sigue a su ritmo
// configurado sin enterarse.
//
// Si el sensor no contesta se cae al ultimo valor empujado en vez de enseñar
// un error: sigue siendo informacion util, solo que mas vieja.
function calVivoArrancar(){
  const pinta = async () => {
    if(!$c('calbg') || $c('calbg').style.display!=='flex') return;
    const e = $c('calvivo'); if(!e) return;
    try{
      const r = await fetch(`/api/tank/${CALID}/vivo`);
      if(r.ok){
        const d = await r.json();
        e.textContent = d.count;
        e.title = 'leido del sensor ahora mismo';
        return;
      }
    }catch(err){}
    try{
      const d = await (await fetch('/api/tanks')).json();
      const t = (d.tanks||[]).find(x=>x.tank_id===CALID);
      e.textContent = (t && t.count!=null) ? t.count : '—';
      e.title = 'ultimo valor recibido: el sensor no responde a la lectura directa';
    }catch(err){}
  };
  pinta();
  if(CALVIVO) clearInterval(CALVIVO);
  CALVIVO = setInterval(pinta, 700);
}
function calVivoParar(){
  if(CALVIVO){ clearInterval(CALVIVO); CALVIVO=null; }
  // Soltar la sesion Modbus en cuanto se sabe que ya nadie mira. La pasarela
  // la cerraria sola por inactividad, pero no tiene sentido dejarla abierta
  // 30 s contra un sensor con el que ya no se esta trabajando.
  if(CALID !== null){ fetch(`/api/tank/${CALID}/soltar`).catch(()=>{}); }
  // Tambien el sondeo del firmware, y se vuelve a geometria: si no, abrir el
  // modal de OTRO tanque lo dejaria en la pestana Firmware, con el boton de
  // actualizar apuntando ya a un sensor distinto del que se estaba mirando.
  if(FWTIMER){ clearTimeout(FWTIMER); FWTIMER=null; }
  if(CALMODO !== 'geo' && $c('mg_geo')) calModo('geo');
}
['ca_c','ca_l','cb_c','cb_l','c_u','g_mm','g_h'].forEach(id=>{
  const e=$c(id); if(e) e.oninput=calCalc; });
if($c('g_s')) $c('g_s').onchange=calCalc;
if($c('ca_now')) $c('ca_now').onclick=()=>calNow('ca_c');
if($c('cb_now')) $c('cb_now').onclick=()=>calNow('cb_c');
if($c('m_n1')) $c('m_n1').onclick=()=>calNow('m_c1');
if($c('m_n2')) $c('m_n2').onclick=()=>calNow('m_c2');
if($c('m_go')) $c('m_go').onclick=geoMedir;
if($c('mg_geo')) $c('mg_geo').onclick=()=>calModo('geo');
if($c('mg_2p'))  $c('mg_2p').onclick =()=>calModo('2p');
if($c('mg_fw'))  $c('mg_fw').onclick =()=>calModo('fw');

// ---------------- firmware del sensor abierto en el modal ----------------
// Se sondea cada 3 s mientras hay un trabajo y cada 20 s en reposo, y SOLO
// con la pestana a la vista: la secuencia dura cerca de minuto y medio y el
// operador necesita ver en que paso va, no un boton gris que no dice nada.
function fwMsg(t, err){
  const d = $c('fw_msg');
  d.textContent = t || '';
  d.style.color = err ? '#f85149' : '';
}

async function fwPinta(){
  if(FWTIMER){ clearTimeout(FWTIMER); FWTIMER = null; }
  if(CALMODO !== 'fw' || CALID === null) return;
  try{
    const j = await (await fetch('/api/ota')).json();
    const im = j.imagen, t = j.trabajo;
    $c('fw_img').innerHTML = im
      ? `imagen cargada: <b>v${im.version}</b> · ${(im.tam/1024).toFixed(1)} kB`
        + ` · <span class="mut">sha ${im.sha.slice(0,16)}…</span>`
      : 'no hay ninguna imagen cargada';

    // El trabajo es global (uno a la vez). Si el que corre es de OTRO tanque
    // se dice con su numero: si no, uno ve "subiendo la imagen" en el modal
    // del tanque 7 creyendo que es el suyo.
    const mio = t && String(t.tank_id) === String(CALID);
    const enCurso = t && t.estado === 'en_curso';
    if(t){
      const col = t.estado==='ok' ? '#3fb950' : (t.estado==='error' ? '#f85149' : '#d29922');
      $c('fw_est').innerHTML = `<span style="color:${col}">`
        + (mio ? '' : `tanque ${t.tank_id}: `)
        + `${t.fase}${t.motivo ? ' — '+t.motivo : ''}</span>`;
    } else {
      $c('fw_est').textContent = '';
    }
    $c('fw_go').disabled = !im || enCurso;
    FWTIMER = setTimeout(fwPinta, enCurso ? 3000 : 20000);
  }catch(e){
    $c('fw_img').textContent = 'sin datos de firmware';
    FWTIMER = setTimeout(fwPinta, 20000);
  }
}

if($c('fw_up')) $c('fw_up').onclick = async()=>{
  const f = $c('fw_file').files[0];
  if(!f){ fwMsg('elige primero un fichero .bin', true); return; }
  fwMsg('subiendo '+f.name+'…');
  try{
    const r = await fetch('/api/ota/imagen', {method:'POST', body:f});
    const j = await r.json();
    if(!r.ok){ fwMsg(j.error || 'rechazada', true); return; }
    fwMsg('imagen cargada y verificada');
    fwPinta();
  }catch(e){ fwMsg('error al subir: '+e, true); }
};

if($c('fw_go')) $c('fw_go').onclick = async()=>{
  if(CALID === null) return;
  if(!confirm('¿Actualizar el firmware del tanque '+CALID+'?\n\n'
      +'La imagen nueva arranca a prueba y se revierte sola si el sensor no '
      +'transmite. Tarda alrededor de minuto y medio.')) return;
  $c('fw_go').disabled = true;
  try{
    const r = await fetch('/api/ota/tank/'+CALID, {method:'POST'});
    const j = await r.json();
    if(!r.ok){ fwMsg(j.error || 'no se pudo lanzar', true); $c('fw_go').disabled=false; return; }
    fwMsg('');
    fwPinta();
  }catch(e){ fwMsg('error: '+e, true); $c('fw_go').disabled=false; }
};
if($c('g_ing')) $c('g_ing').onclick=()=>{ $c('g_mm').value=MMP_INGLESA.toFixed(6); calCalc(); };
if($c('g_met')) $c('g_met').onclick=()=>{ $c('g_mm').value=MMP_METRICA.toFixed(6); calCalc(); };
if($c('led_on'))  $c('led_on').onclick  = ()=>accion('ledson');
if($c('led_off')) $c('led_off').onclick = ()=>accion('ledsoff');
if($c('calclose')) $c('calclose').onclick=()=>{ $c('calbg').style.display='none'; calVivoParar(); };
if($c('calbg')) $c('calbg').onclick=e=>{ if(e.target===$c('calbg')){ $c('calbg').style.display='none'; calVivoParar(); } };
if($c('calsave')) $c('calsave').onclick=async()=>{
  const r=calCalc(); if(!r) return;
  // ⚠️ Se manda la ORDEN, no la recta ya resuelta. Es la misma estructura que
  // acepta el topico MQTT de calibracion y la resuelve la MISMA funcion en la
  // TPU. Si el panel calculara y guardara por su cuenta, panel y sala de
  // visualizacion serian dos implementaciones de la misma regla, y tarde o
  // temprano una validaria algo que la otra no.
  const orden = (CALMODO==='geo')
    ? {modo:'geometrica', mm_por_pulso:r.mm_por_pulso,
       altura_referencia:r.altura_referencia, sentido:r.sentido,
       unidad:$c('c_u').value}
    : {modo:'dos_puntos', unidad:$c('c_u').value,
       punto_a:{pulsos:+$c('ca_c').value, nivel:+$c('ca_l').value},
       punto_b:{pulsos:+$c('cb_c').value, nivel:+$c('cb_l').value}};
  const res=await fetch('/api/tank/'+CALID+'/config',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify(orden)});
  const d=await res.json();
  if(res.ok){
    // Con aviso NO se cierra: guardar y desaparecer dejaria el aviso sin leer,
    // y avisar de algo que nadie ve es lo mismo que no avisar.
    if(d.aviso){ $c('calm').innerHTML='<span style="color:#ffd479">Guardado, pero: '
      + d.aviso+'</span>'; return; }
    $c('calbg').style.display='none'; calVivoParar(); tanks();
  }
  else $c('calm').textContent='No se guardo: '+(d.error||'error');
};

function render(){
  $('#secs').innerHTML = Object.entries(SECS).map(([k,s])=>{
    const c=CFG[k]||{}, on = s.nosw ? true : !!c.enabled;
    // Identificacion no se apaga: no tiene interruptor.
    const sw = s.nosw ? '' : `<label class="sw"><input type="checkbox" data-k="${k}"
        data-f="enabled" ${on?'checked':''}><span class="sl"></span></label>`;
    return `<div class="card"><div class="hd">${sw}
      <h2 style="text-transform:none;font-size:15px;color:var(--fg)">${s.t}</h2></div>
      ${s.f.map(([f,lab,ty,opts])=>{
        const v=c[f]??'';
        let inp;
        if(ty=='bool') inp=`<label class="sw"><input type="checkbox" data-k="${k}"
            data-f="${f}" ${v?'checked':''} ${on?'':'disabled'}><span class="sl"></span></label>`;
        else if(ty=='sel') inp=`<select data-k="${k}" data-f="${f}" ${on?'':'disabled'}>`+
            opts.map(o=>`<option ${o==v?'selected':''}>${o}</option>`).join('')+`</select>`;
        else inp=`<input type="${ty}" data-k="${k}" data-f="${f}" value="${v}"
            ${on?'':'disabled'}>`;
        return `<div class="row"><label>${lab}</label>${inp}</div>`;
      }).join('')}</div>`;
  }).join('');
  document.querySelectorAll('[data-f=enabled]').forEach(x=>x.onchange=()=>{
    CFG[x.dataset.k].enabled = x.checked; render();
  });
}

async function load(){
  const r=await fetch('/api/config');
  if(r.status==401){ $('#m').className='msg err';
    $('#m').textContent='Credenciales incorrectas.'; return; }
  CFG=await r.json(); render();
  const nm = (CFG.site&&CFG.site.name||'').trim();
  if(nm){ $('#site').textContent = nm; document.title = nm; }
}

$('#save').onclick=async()=>{
  const out={};
  document.querySelectorAll('[data-k]').forEach(x=>{
    const k=x.dataset.k,f=x.dataset.f;
    out[k]=out[k]||{};
    out[k][f] = x.type=='checkbox' ? x.checked
              : x.type=='number'   ? parseInt(x.value||'0',10)
              : (x.tagName=='SELECT' && !isNaN(x.value) && x.value!=='')
                                   ? parseInt(x.value,10) : x.value;
  });
  $('#save').disabled=true;
  const r=await fetch('/api/config',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(out)});
  const d=await r.json();
  const m=$('#m');
  if(r.ok){ m.className='msg ok'; m.textContent='Guardado. Reiniciando el servicio…';
    setTimeout(()=>{ location.reload(); },6000);
  } else { m.className='msg err'; m.textContent='No se guardó: '+(d.error||'error');
    $('#save').disabled=false; }
};

// ---- card de la TPU -------------------------------------------------
// Sale del mismo /api/arbol que se publica por MQTT: una sola fuente, para que
// el panel y el consumidor externo no puedan contar cosas distintas.
async function tpu(){
  try{
    const r = await fetch('/api/arbol'); const d = await r.json();
    const t = d.tpu||{}, al = d.almacenamiento||{}, amb = t.ambiente||{};
    const v = (x,u) => (x===null||x===undefined) ? '<span class="mut">—</span>' : x+u;
    const cal = c => (c===null||c===undefined) ? '' :
        (c>=75 ? ' style="color:#e5534b;font-weight:600"' :
         c>=65 ? ' style="color:#d29922"' : '');
    $('#tpu_hdr').textContent = d.raiz && d.raiz.nombre ? d.raiz.nombre : '';
    const alm = al.disponible === false
      ? '<span style="color:#e5534b;font-weight:600">SIN ALMACENAMIENTO</span>'
        + (al.muestras_perdidas ? ' <span class="mut">('+al.muestras_perdidas+' muestras perdidas)</span>' : '')
      : (al.disponible === true ? '<span style="color:#3fb950">guardando</span>'
                                : '<span class="mut">—</span>');
    $('#tpu_wrap').innerHTML = `<table><thead><tr>
        <th>Marcha</th>
        <th>SoC</th><th>NVMe</th><th>RP1</th><th>Ventilador</th><th>Tension</th>
        <th>Ambiente</th><th>eth0 (planta)</th><th>Historico</th></tr></thead><tbody><tr>
        <td class="nw">${marcaMarcha(t.uptime_s)}<br><span class="mut" style="font-size:11px"
             title="marcha del proceso de la pasarela">pasarela ${marcha(t.pasarela_s)}</span></td>
        <td class="nw"${cal(t.soc_c)}>${v(t.soc_c,' °C')}</td>
        <td class="nw"${cal(t.nvme_c)}>${v(t.nvme_c,' °C')}</td>
        <td class="nw"${cal(t.rp1_c)}>${v(t.rp1_c,' °C')}</td>
        <td class="nw">${v(t.ventilador_rpm,' rpm')}</td>
        <td>${t.subtension === true
              ? '<span style="color:#e5534b;font-weight:600">SUBTENSION</span>'
              : (t.subtension === false ? '<span style="color:#3fb950">OK</span>'
                                        : '<span class="mut">—</span>')}</td>
        <td>${(amb.temp_c===null||amb.temp_c===undefined)
              ? '<span class="mut">sin sensor</span>'
              : amb.temp_c+' °C / '+amb.humi_rh+' %'}</td>
        <td>${(()=>{ const e=(t.red||{}).eth0||{};
              if(e.ip) return e.ip + (e.mbps?' <span class="mut">('+e.mbps+' Mbps)</span>':'');
              return e.enlace ? '<span class="mut">enlace sin IP</span>'
                              : '<span class="mut">sin enlace</span>'; })()}</td>
        <td>${alm}</td></tr></tbody></table>`;
  }catch(e){
    $('#tpu_wrap').innerHTML = '<p class="mut">sin datos de la TPU</p>';
  }
}


tanks(); setInterval(tanks,5000);
tpu();   setInterval(tpu,10000);
load();
</script></body></html>
"""
