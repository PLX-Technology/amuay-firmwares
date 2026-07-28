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
 main{max-width:1000px;margin:20px auto;padding:0 16px}
 .card{background:var(--card);border:1px solid var(--line);border-radius:8px;
       padding:16px;margin-bottom:14px}
 .card h2{font-size:14px;margin:0 0 12px;text-transform:uppercase;letter-spacing:.04em;
          color:var(--mut);display:flex;align-items:center;gap:8px}
 table{width:100%;border-collapse:collapse}
 th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);font-size:14px}
 th{color:var(--mut);font-weight:600;font-size:12px;text-transform:uppercase}
 .dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:6px}
 .up{background:var(--ok)} .down{background:var(--bad)}
 .row{display:grid;grid-template-columns:150px 1fr;gap:10px;align-items:center;
      margin-bottom:9px}
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
      <h2>Tanques</h2>
      <table><thead><tr><th>ID</th><th>Nombre</th><th>MAC</th><th>Valor</th><th>Pulsos</th><th>Errores</th>
        <th>Última conexión</th><th>Estado</th><th></th></tr></thead><tbody id="tb">
        <tr><td colspan="9" class="mut">cargando…</td></tr></tbody></table>
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
document.querySelectorAll('.tab').forEach(x=>x.onclick=()=>{
  document.querySelectorAll('.tab').forEach(y=>y.classList.toggle('on',y===x));
  $('#dash').style.display = x.dataset.t=='dash'?'':'none';
  $('#cfg').style.display  = x.dataset.t=='cfg'?'':'none';
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

async function tanks(){
  // No repintar mientras se edita: borraria lo que el usuario esta escribiendo.
  if(document.querySelector('.tid:focus')) return;
  try{
    const r=await fetch('/api/tanks'); const d=await r.json();
    $('#hdr').textContent = d.n+' tanque'+(d.n==1?'':'s');
    $('#tb').innerHTML = d.tanks.length ? d.tanks.map(t=>`<tr>
      <td><input type="number" class="tid" value="${t.tank_id}" data-old="${t.tank_id}"
           style="width:80px"></td>
      <td>${t.name||''}</td>
      <td class="mut" style="font-family:ui-monospace,monospace;font-size:12px">${t.mac||'—'}</td>
      <td><b>${t.value!=null? t.value.toFixed(2) : '—'}</b> <span class="mut">${t.unit||''}</span></td>
      <td>${t.count??0}</td>
      <td>${t.errors??0}</td><td>${fechaHora(t.ts)}<br><span class="mut" style="font-size:11px">${t.age_s!=null? 'hace '+hace(t.age_s) : ''}</span></td>
      <td><span class="dot ${t.online?'up':'down'}"></span>${t.online?'en línea':'sin señal'}</td>
      <td><button class="sid" data-id="${t.tank_id}" style="padding:5px 10px;font-size:13px"
           disabled>Guardar</button></td>
      </tr>`).join('') :
      '<tr><td colspan="9" class="mut">ningún tanque dado de alta todavía</td></tr>';
    // El boton solo se activa si el valor cambio: evita escrituras accidentales
    // a la EEPROM del sensor.
    document.querySelectorAll('.tid').forEach(x=>x.oninput=()=>{
      const b=x.closest('tr').querySelector('.sid');
      b.disabled = (x.value==x.dataset.old || !x.value);
    });
    document.querySelectorAll('.sid').forEach(b=>b.onclick=()=>saveId(b));
  }catch(e){ $('#hdr').textContent='sin conexión'; }
}

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

tanks(); setInterval(tanks,5000); load();
</script></body></html>
"""
