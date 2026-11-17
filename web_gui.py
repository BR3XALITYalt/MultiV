"""Password-protected browser control panel for TXITPY.

The server uses only the Python standard library. Credentials are stored as a
PBKDF2 hash, authenticated sessions are held in memory, and state-changing
requests require a per-session CSRF token.
"""
from __future__ import annotations

import hashlib
import hmac
import http.cookies
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

PASSWORD_ITERATIONS = 600_000
SESSION_SECONDS = 12 * 60 * 60
MAX_REQUEST_BYTES = 64 * 1024
MAX_LOGIN_ATTEMPTS = 6
LOGIN_WINDOW_SECONDS = 60


def create_password_record(password: str) -> tuple[str, str]:
    """Return a random salt and PBKDF2-SHA256 password digest as hex strings."""
    if not isinstance(password, str) or len(password) < 12:
        raise ValueError("Use a password with at least 12 characters.")
    if len(password) > 256:
        raise ValueError("Password must be 256 characters or fewer.")
    salt = secrets.token_bytes(32)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_ITERATIONS,
    )
    return salt.hex(), digest.hex()


def verify_password_record(password: str, salt_hex: str, digest_hex: str) -> bool:
    """Verify a password record without exposing the stored digest."""
    if not isinstance(password, str) or not salt_hex or not digest_hex:
        return False
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
        if len(salt) != 32 or len(expected) != 32:
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            PASSWORD_ITERATIONS,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


LOGIN_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TXITY - TXITPY Login</title>
<style>
:root{color-scheme:light;--blue:#1769aa;--ink:#202124;--muted:#5f6368;--line:#dadce0;--surface:#fff;--page:#f5f7fa}
*{box-sizing:border-box}body{margin:0;background:var(--page);color:var(--ink);font:15px Arial,Helvetica,sans-serif}
header{height:62px;background:#fff;border-bottom:1px solid var(--line);display:flex;align-items:center;padding:0 max(22px,calc((100% - 1060px)/2));gap:28px}
.brand{font-size:21px;font-weight:700;letter-spacing:.2px;color:#1a1a1a}
nav{display:flex;gap:8px}nav a{color:#41464d;text-decoration:none;padding:10px 13px;border-radius:4px}
nav a:first-child{background:#e8f0fe;color:#174ea6}
main{max-width:1060px;margin:42px auto;padding:0 22px}
h1{font-size:27px;font-weight:600;margin:0 0 8px}p{color:var(--muted);line-height:1.5}
.card{max-width:460px;background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:24px;margin-top:24px}
label{display:block;font-size:13px;font-weight:600;margin:18px 0 7px}
input{width:100%;padding:12px;border:1px solid #b9bec5;border-radius:4px;font:inherit}
button{margin-top:18px;background:var(--blue);color:white;border:0;border-radius:4px;padding:11px 18px;font:inherit;font-weight:600;cursor:pointer}
.error{color:#b3261e}
</style>
</head>
<body>
<header><div class="brand">TXITY</div><nav><a href="/">Transmit</a></nav></header>
<main><h1>TXITPY control panel</h1><p>Sign in to manage your TXITPY transmitter and station profile.</p>
<section class="card"><h2>Local control login</h2><form method="post" action="/login">
<label for="password">Password</label><input id="password" name="password" type="password" autocomplete="current-password" required autofocus>
<button type="submit">Sign in</button></form>
<p class="error">__ERROR__</p></section></main>
</body></html>"""


DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="csrf-token" content="__CSRF__">
<title>TXITY - TXITPY</title>
<style>
:root{color-scheme:light;--blue:#1769aa;--blue-dark:#174ea6;--ink:#202124;--muted:#5f6368;--line:#dadce0;--surface:#fff;--page:#f5f7fa;--pale:#e8f0fe;--green:#137333;--red:#b3261e}
*{box-sizing:border-box}body{margin:0;background:var(--page);color:var(--ink);font:15px Arial,Helvetica,sans-serif}
header{min-height:62px;background:#fff;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;padding:7px max(22px,calc((100% - 1120px)/2));gap:16px}
.brand{font-size:21px;font-weight:700;letter-spacing:.2px;color:#1a1a1a;white-space:nowrap}
nav{display:flex;align-items:center;gap:5px;flex-wrap:wrap}nav a,nav button{color:#41464d;text-decoration:none;padding:10px 13px;border:0;background:transparent;border-radius:4px;font:inherit;cursor:pointer}
nav a.active{background:var(--pale);color:var(--blue-dark);font-weight:600}
main{max-width:1120px;margin:26px auto 60px;padding:0 22px}
.hero{display:flex;justify-content:space-between;align-items:center;gap:16px;margin:8px 0 20px}
h1{font-size:27px;font-weight:600;margin:0 0 5px}h2{font-size:19px;font-weight:600;margin:0 0 16px}h3{font-size:15px;font-weight:600;margin:0 0 8px}
.sub{color:var(--muted);margin:0;line-height:1.45}.status{display:inline-flex;align-items:center;gap:8px;color:var(--muted);font-size:13px}
.dot{width:9px;height:9px;border-radius:50%;background:#8a8f98}.dot.live{background:#188038}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:20px;margin:0 0 14px;min-width:0}
.field-grid{display:grid;grid-template-columns:1fr 1fr;gap:13px}
.field{min-width:0}.field.full{grid-column:1/-1}
label{display:block;color:#4b5057;font-size:13px;font-weight:600;margin:0 0 7px}
input,select,textarea{width:100%;padding:10px 11px;border:1px solid #b9bec5;border-radius:4px;background:#fff;color:var(--ink);font:inherit;min-height:41px}
textarea{min-height:150px;resize:vertical;line-height:1.45}
input:focus,select:focus,textarea:focus{outline:2px solid #8ab4f8;outline-offset:1px;border-color:var(--blue)}
.hint{font-size:12px;color:var(--muted);line-height:1.45;margin:6px 0 0}
.actions{display:flex;gap:9px;align-items:center;flex-wrap:wrap;margin-top:17px}
button.primary{background:var(--blue);color:white;border:1px solid var(--blue);border-radius:4px;padding:10px 17px;font:inherit;font-weight:600;cursor:pointer}
button.secondary{background:white;color:var(--blue-dark);border:1px solid #aeb8c4;border-radius:4px;padding:10px 15px;font:inherit;cursor:pointer}
button:disabled{opacity:.5;cursor:not-allowed}
.checkbox{display:flex;align-items:center;gap:9px;color:var(--ink);font-weight:400}.checkbox input{width:17px;min-height:17px;margin:0}
#notice{min-height:23px;color:var(--muted);font-size:13px;margin-top:8px}
#notice.error{color:var(--red)}#notice.success{color:var(--green)}
.state-box{padding:12px 14px;background:#f8f9fa;border:1px solid var(--line);border-radius:5px;color:#3c4043;line-height:1.5}
footer{max-width:1120px;margin:0 auto;padding:0 22px 28px;color:var(--muted);font-size:12px}
@media(max-width:720px){header{align-items:flex-start;flex-direction:column;padding:14px 18px;gap:8px}.grid,.field-grid{grid-template-columns:1fr}.field.full{grid-column:auto}.hero{align-items:flex-start;flex-direction:column}main{margin-top:20px;padding:0 14px}}
</style>
</head>
<body>
<header><div class="brand">TXITY</div><nav>
<a class="active" href="#transmit">Transmit</a><a href="#profile">Station profile</a><a href="#page-details">Page details</a>
<button type="button" id="logout">Logout</button>
</nav></header>
<main>
<div class="hero"><div><h1>TXITY</h1><p class="sub">TXITPY remote station control</p></div>
<div class="status"><span id="live-dot" class="dot"></span><span id="status">Connecting to TXITPY...</span></div></div>
<form id="control-form">
<section id="transmit" class="card">
<h2>Station and transmission</h2>
<div class="field-grid">
<div class="field"><label for="station_name">Station name</label><input id="station_name" maxlength="48" required></div>
<div class="field"><label for="description">Short description</label><input id="description" maxlength="180"></div>
<div class="field"><label for="frequency">Transmit frequency</label><input id="frequency" maxlength="32" placeholder="7.250 MHz" required></div>
<div class="field"><label for="mode">Signal type</label><select id="mode"><option>AM</option><option>AM (DEV)</option><option>USB</option><option>LSB</option><option>USB+C</option><option>LSB+C</option><option>DSB</option><option>NFM</option><option>DRM</option><option>CW</option></select></div>
<div class="field"><label for="station_dbm">Station power (dBm)</label><input id="station_dbm" type="number" min="-140" max="53" step="1"></div>
<div class="field"><label>Audio source</label><div id="audio-source" class="state-box">Selected in the TXITPY desktop app</div><p class="hint">Select the microphone, desktop audio, or playlist in the desktop app.</p></div>
<div class="field full"><label class="checkbox"><input id="allow_overlap" type="checkbox"> Allow frequency sharing with other stations</label></div>
</div>
<div class="actions"><button class="primary" id="transmit-button" type="button">Start transmission</button><button class="secondary" type="submit">Apply settings</button></div>
<div id="notice" role="status" aria-live="polite"></div>
</section>
<section id="profile" class="card">
<h2>Station profile</h2>
<div class="field-grid">
<div class="field full"><label for="station_logo_url">Station logo</label><input id="station_logo_url" type="url" maxlength="512" placeholder="https://example.com/logo.png"><p class="hint">Enter a public image URL. TXITY accepts PNG, JPEG, GIF, or WebP station logos.</p></div>
<div class="field full" id="page-details"><label for="station_details">Station page details</label><textarea id="station_details" maxlength="1200" placeholder="Information displayed on your public TXITY station page."></textarea><p class="hint">Shown on the public station page when visitors open your station. Maximum 1,200 characters.</p></div>
</div>
<div class="actions"><button class="secondary" type="submit">Save station profile</button></div>
</section>
</form>
</main>
<footer>TXITPY browser control · TXITY station configuration</footer>
<script>
const csrf=document.querySelector('meta[name="csrf-token"]').content;
const fields=['station_name','description','frequency','mode','station_dbm','station_logo_url','station_details'];
let currentState={transmitting:false};
let initialised=false;
let lastServerNotice='';
function value(id){return document.getElementById(id).value;}
function setValue(id,v){const e=document.getElementById(id);if(e&&document.activeElement!==e)e.value=v==null?'':String(v);}
function notice(message,kind){const e=document.getElementById('notice');e.textContent=message;e.className=kind||'';}
async function request(path,data){
 const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(data)});
 const result=await response.json().catch(()=>({message:'Unexpected server response.'}));
 if(!response.ok)throw new Error(result.message||'Request failed.');
 return result;
}
async function refresh(){
 try{
  const response=await fetch('/api/state',{cache:'no-store'});
  if(!response.ok)throw new Error('Session expired. Sign in again.');
  const s=await response.json();currentState=s;
  if(s.notice && s.notice!==lastServerNotice){notice(s.notice);lastServerNotice=s.notice;}
  document.getElementById('status').textContent=s.status||'Connected';
  document.getElementById('live-dot').className='dot'+(s.transmitting?' live':'');
  document.getElementById('transmit-button').textContent=s.transmitting?'Stop transmission':'Start transmission';
  document.getElementById('audio-source').textContent=s.audio_source||'Selected in the TXITPY desktop app';
  if(!initialised){
   fields.forEach(k=>setValue(k,s[k]));
   document.getElementById('allow_overlap').checked=!!s.allow_overlap;
   initialised=true;
  }
 }catch(error){notice(error.message,'error');}
}
document.getElementById('control-form').addEventListener('submit',async event=>{
 event.preventDefault();
 const data={};
 fields.forEach(k=>data[k]=value(k));
 data.allow_overlap=document.getElementById('allow_overlap').checked;
 try{await request('/api/settings',data);notice('Settings queued for TXITPY.','success');setTimeout(refresh,300);}
 catch(error){notice(error.message,'error');}
});
document.getElementById('transmit-button').addEventListener('click',async()=>{
 const action=currentState.transmitting?'stop':'start';
 try{await request('/api/transmit',{action});notice(action==='start'?'Transmission start requested.':'Transmission stop requested.','success');setTimeout(refresh,300);}
 catch(error){notice(error.message,'error');}
});
document.getElementById('logout').addEventListener('click',async()=>{
 try{await request('/logout',{});}catch(error){}
 window.location='/login';
});
refresh();setInterval(refresh,1500);
</script>
</body></html>"""


class TxityWebGUI:
    """Run a small authenticated HTTP control panel for the desktop client."""

    def __init__(
        self,
        *,
        host: str = "0.0.0.0",
        port: int = 23234,
        password_salt: str,
        password_hash: str,
        state_provider: Callable[[], dict[str, Any]],
        command_submitter: Callable[[dict[str, Any]], None],
    ) -> None:
        self.host = host
        self.port = int(port)
        self._password_salt = str(password_salt or "")
        self._password_hash = str(password_hash or "")
        self._state_provider = state_provider
        self._command_submitter = command_submitter
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._sessions: dict[str, dict[str, Any]] = {}
        self._login_attempts: dict[str, tuple[float, int]] = {}

    @property
    def running(self) -> bool:
        return self._server is not None and self._thread is not None and self._thread.is_alive()

    @property
    def address(self) -> tuple[str, int] | None:
        """Return the bound address, including an OS-selected port when port=0."""
        with self._lock:
            return self._server.server_address if self._server is not None else None

    def set_credentials(self, salt: str, digest: str) -> None:
        """Replace the password record and invalidate existing browser sessions."""
        with self._lock:
            self._password_salt = str(salt or "")
            self._password_hash = str(digest or "")
            self._sessions.clear()

    def start(self) -> None:
        with self._lock:
            if self.running:
                return
            if not self._password_salt or not self._password_hash:
                raise ValueError("Set a Web GUI password in TXITPY Settings before enabling the server.")
            owner = self

            class Handler(BaseHTTPRequestHandler):
                server_version = "TXITPYWeb/1.0"
                sys_version = ""

                def log_message(self, _format: str, *args: Any) -> None:
                    return

                def _headers(self, content_type: str, length: int, cookie: str | None = None) -> None:
                    self.send_response(200)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(length))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Referrer-Policy", "no-referrer")
                    self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src https: data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
                    if cookie:
                        self.send_header("Set-Cookie", cookie)
                    self.end_headers()

                def _send(self, status: int, body: bytes, content_type: str = "text/html; charset=utf-8", cookie: str | None = None) -> None:
                    self.send_response(status)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Referrer-Policy", "no-referrer")
                    self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src https: data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
                    if cookie:
                        self.send_header("Set-Cookie", cookie)
                    self.end_headers()
                    self.wfile.write(body)

                def _json(self, status: int, value: dict[str, Any]) -> None:
                    body = json.dumps(value, ensure_ascii=False).encode("utf-8")
                    self._send(status, body, "application/json; charset=utf-8")

                def _body(self) -> bytes:
                    raw_length = self.headers.get("Content-Length", "0")
                    try:
                        length = int(raw_length)
                    except ValueError:
                        raise ValueError("Invalid request length.")
                    if length < 0 or length > MAX_REQUEST_BYTES:
                        raise ValueError("Request is too large.")
                    return self.rfile.read(length)

                def _json_body(self) -> dict[str, Any]:
                    raw = self._body()
                    try:
                        value = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise ValueError("Invalid JSON request.") from exc
                    if not isinstance(value, dict):
                        raise ValueError("Request body must be an object.")
                    return value

                def _session(self) -> tuple[str | None, dict[str, Any] | None]:
                    cookie_header = self.headers.get("Cookie", "")
                    cookie = http.cookies.SimpleCookie()
                    try:
                        cookie.load(cookie_header)
                        morsel = cookie.get("txitpy_session")
                        session_id = morsel.value if morsel else None
                    except http.cookies.CookieError:
                        session_id = None
                    if not session_id:
                        return None, None
                    with owner._lock:
                        session = owner._sessions.get(session_id)
                        if not session:
                            return session_id, None
                        if session["expires"] <= time.time():
                            owner._sessions.pop(session_id, None)
                            return session_id, None
                        session["expires"] = time.time() + SESSION_SECONDS
                        return session_id, dict(session)

                def _require_session(self) -> tuple[str, dict[str, Any]] | None:
                    session_id, session = self._session()
                    if not session_id or not session:
                        self._json(401, {"ok": False, "message": "Please sign in again."})
                        return None
                    return session_id, session

                def _require_csrf(self, session: dict[str, Any]) -> bool:
                    supplied = self.headers.get("X-CSRF-Token", "")
                    if not supplied or not hmac.compare_digest(supplied, str(session["csrf"])):
                        self._json(403, {"ok": False, "message": "Invalid request token. Reload the page and try again."})
                        return False
                    return True

                def do_GET(self) -> None:
                    path = urlsplit(self.path).path
                    if path in {"/", "/index.html"}:
                        session_id, session = self._session()
                        if not session_id or not session:
                            self._send(200, LOGIN_HTML.replace("__ERROR__", "").encode("utf-8"))
                            return
                        page = DASHBOARD_HTML.replace("__CSRF__", str(session["csrf"]))
                        self._send(200, page.encode("utf-8"))
                        return
                    if path == "/login":
                        self._send(200, LOGIN_HTML.replace("__ERROR__", "").encode("utf-8"))
                        return
                    if path == "/api/state":
                        if not self._require_session():
                            return
                        try:
                            state = owner._state_provider()
                        except Exception:
                            state = {"status": "TXITPY state is temporarily unavailable."}
                        self._json(200, state if isinstance(state, dict) else {})
                        return
                    self._send(404, b"Not found", "text/plain; charset=utf-8")

                def do_POST(self) -> None:
                    path = urlsplit(self.path).path
                    if path == "/login":
                        try:
                            raw = self._body()
                            form = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
                            password = form.get("password", [""])[0]
                        except (ValueError, UnicodeDecodeError):
                            self._send(400, b"Invalid login request", "text/plain; charset=utf-8")
                            return
                        remote = self.client_address[0] if self.client_address else "unknown"
                        now = time.time()
                        with owner._lock:
                            started, attempts = owner._login_attempts.get(remote, (now, 0))
                            if now - started >= LOGIN_WINDOW_SECONDS:
                                started, attempts = now, 0
                            blocked = attempts >= MAX_LOGIN_ATTEMPTS
                            if not blocked:
                                owner._login_attempts[remote] = (started, attempts + 1)
                            salt, digest = owner._password_salt, owner._password_hash
                        if blocked:
                            self._send(429, LOGIN_HTML.replace("__ERROR__", "Too many attempts. Try again in one minute.").encode("utf-8"))
                            return
                        if not verify_password_record(password, salt, digest):
                            self._send(401, LOGIN_HTML.replace("__ERROR__", "Incorrect password.").encode("utf-8"))
                            return
                        session_id = secrets.token_urlsafe(32)
                        csrf = secrets.token_urlsafe(32)
                        with owner._lock:
                            owner._sessions[session_id] = {
                                "csrf": csrf,
                                "expires": time.time() + SESSION_SECONDS,
                            }
                            owner._login_attempts.pop(remote, None)
                        body = b"<!doctype html><meta http-equiv='refresh' content='0;url=/'><a href='/'>Continue</a>"
                        self._send(
                            200,
                            body,
                            "text/html; charset=utf-8",
                            f"txitpy_session={session_id}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_SECONDS}",
                        )
                        return

                    if path == "/logout":
                        checked = self._require_session()
                        if not checked:
                            return
                        session_id, session = checked
                        if not self._require_csrf(session):
                            return
                        with owner._lock:
                            owner._sessions.pop(session_id, None)
                        self._json(200, {"ok": True})
                        return

                    if path in {"/api/settings", "/api/transmit"}:
                        checked = self._require_session()
                        if not checked:
                            return
                        _session_id, session = checked
                        if not self._require_csrf(session):
                            return
                        try:
                            data = self._json_body()
                        except ValueError as exc:
                            self._json(400, {"ok": False, "message": str(exc)})
                            return
                        if path == "/api/settings":
                            limits = {
                                "station_name": 48,
                                "description": 180,
                                "frequency": 32,
                                "station_dbm": 16,
                                "station_logo_url": 512,
                                "station_details": 1200,
                            }
                            values: dict[str, Any] = {}
                            for key, maximum in limits.items():
                                value = data.get(key, "")
                                if not isinstance(value, str) or len(value) > maximum:
                                    self._json(400, {"ok": False, "message": f"Invalid {key.replace('_', ' ')} value."})
                                    return
                                values[key] = value
                            mode = data.get("mode", "AM")
                            if mode not in {"AM", "AM (DEV)", "USB", "LSB", "USB+C", "LSB+C", "DSB", "NFM", "DRM", "CW"}:
                                self._json(400, {"ok": False, "message": "Unsupported signal type."})
                                return
                            values["mode"] = mode
                            overlap = data.get("allow_overlap", False)
                            if not isinstance(overlap, bool):
                                self._json(400, {"ok": False, "message": "Invalid frequency-sharing setting."})
                                return
                            values["allow_overlap"] = overlap
                            try:
                                power = float(values["station_dbm"])
                            except ValueError:
                                self._json(400, {"ok": False, "message": "Station power must be a number."})
                                return
                            if not -140 <= power <= 53:
                                self._json(400, {"ok": False, "message": "Station power must be between -140 and 53 dBm."})
                                return
                            values["station_dbm"] = str(power)
                            owner._command_submitter({"type": "settings", "values": values})
                            self._json(202, {"ok": True, "message": "Settings queued for TXITPY."})
                            return

                        action = data.get("action")
                        if action not in {"start", "stop"}:
                            self._json(400, {"ok": False, "message": "Choose start or stop."})
                            return
                        owner._command_submitter({"type": "transmit", "action": action})
                        self._json(202, {"ok": True, "message": f"Transmission {action} requested."})
                        return

                    self._send(404, b"Not found", "text/plain; charset=utf-8")

            self._server = ThreadingHTTPServer((self.host, self.port), Handler)
            self._server.daemon_threads = True
            self._thread = threading.Thread(
                target=self._server.serve_forever,
                kwargs={"poll_interval": 0.25},
                daemon=True,
                name="txitpy-web-gui",
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            server = self._server
            thread = self._thread
            self._server = None
            self._thread = None
            self._sessions.clear()
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
