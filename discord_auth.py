"""Native Discord sign-in handoff for TXITY using the system browser.

TXITY performs Discord OAuth and issues its own signed token. This module opens
that official route in the user's normal browser and receives the resulting URL
fragment through a one-use loopback callback. The token is held in memory only.
"""

from __future__ import annotations

import base64
import hmac
import json
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Queue
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

BASE_URL = "https://txity.radiodiscussion.org"
AUTH_START_URL = f"{BASE_URL}/auth/discord/start"
CALLBACK_TIMEOUT_SECONDS = 300
MAX_CALLBACK_BODY = 12_000


def is_auth_token(value: Any) -> bool:
    """Validate an opaque TXITY token without logging or persisting it."""
    return (
        isinstance(value, str)
        and 16 <= len(value) <= 8192
        and not any(character.isspace() or ord(character) < 32 for character in value)
    )


def clean_discord_user(value: Any) -> dict[str, str] | None:
    """Keep only the public identity fields needed by the desktop UI."""
    if not isinstance(value, dict):
        return None
    user_id = value.get("id")
    username = value.get("username")
    if not isinstance(user_id, str) or not user_id:
        return None
    if not isinstance(username, str) or not username:
        return None

    user = {"id": user_id[:64], "username": username[:128]}
    for key in ("globalName", "avatarUrl"):
        item = value.get(key)
        if isinstance(item, str) and item:
            user[key] = item[:512]
    return user


def user_from_txity_token(token: str) -> dict[str, str] | None:
    """Read the same first-segment identity payload that TXITY's page reads.

    This is only for the account label. The TXITY server remains responsible
    for validating the token signature when the status WebSocket connects.
    """
    if not is_auth_token(token):
        return None
    payload_part = token.split(".", 1)[0]
    try:
        padded = payload_part + "=" * ((4 - len(payload_part) % 4) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    expires = payload.get("exp")
    if isinstance(expires, (int, float)) and expires * 1000 < time.time() * 1000:
        return None
    return clean_discord_user(payload)


def _callback_page(state: str) -> tuple[bytes, str]:
    nonce = secrets.token_urlsafe(18)
    state_js = json.dumps(state).replace("<", "\\u003c")
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TXITPY sign-in</title>
<style>
body{{font:16px system-ui,sans-serif;max-width:36rem;margin:12vh auto;padding:0 1.25rem;color:#171717}}
p{{line-height:1.5;color:#444}}
</style>
</head>
<body>
<h1>Returning to TXITPY</h1>
<p id="message">Completing Discord sign-in…</p>
<script nonce="{nonce}">
(() => {{
  const state = {state_js};
  const params = new URLSearchParams(location.hash.slice(1));
  const token = params.get("discord_auth") || "";
  const joined = params.get("discord_join") === "1";
  history.replaceState(null, "", location.pathname + location.search);
  const message = document.getElementById("message");
  if (!token) {{
    message.textContent = "TXITY did not return a Discord session token. You can close this tab.";
    return;
  }}
  fetch("/complete", {{
    method: "POST",
    headers: {{ "Content-Type": "application/json" }},
    body: JSON.stringify({{ state, token, joined }})
  }}).then(async response => {{
    const result = await response.json().catch(() => ({{}}));
    if (!response.ok || !result.ok) throw new Error(result.error || "Sign-in could not be completed.");
    message.textContent = "Discord sign-in complete. You can return to TXITPY.";
  }}).catch(error => {{
    message.textContent = error instanceof Error ? error.message : "Sign-in could not be completed.";
  }});
}})();
</script>
</body>
</html>"""
    headers = (
        "default-src 'none'; "
        f"script-src 'nonce-{nonce}'; "
        "style-src 'unsafe-inline'; "
        "connect-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )
    return html.encode("utf-8"), headers


class DiscordAuthManager:
    """Open TXITY in the system browser and receive its signed token locally."""

    def __init__(self, events: Queue[dict[str, Any]]) -> None:
        self.events = events
        self._lock = threading.RLock()
        self._server: ThreadingHTTPServer | None = None
        self._state = ""
        self._completed = False
        self._timeout: threading.Timer | None = None
        self.auth_start_url = AUTH_START_URL
        self.secure_auth_enabled = False

    def configure(self, auth_url: Any, secure_enabled: Any) -> None:
        """Accept only TXITY's configured HTTPS auth route."""
        # TXITY's frontend defaults this value to /auth/discord/start when
        # the optional config field is absent.
        if auth_url is None:
            raw_url = "/auth/discord/start"
        elif isinstance(auth_url, str):
            raw_url = auth_url.strip()
        else:
            raw_url = ""
        candidate = urljoin(BASE_URL + "/", raw_url) if raw_url else ""
        try:
            parsed = urlsplit(candidate)
            allowed = (
                bool(raw_url)
                and parsed.scheme == "https"
                and parsed.hostname == "txity.radiodiscussion.org"
                and parsed.port in (None, 443)
            )
        except ValueError:
            allowed = False
        with self._lock:
            self.auth_start_url = candidate if allowed else AUTH_START_URL
            self.secure_auth_enabled = secure_enabled is True and allowed

    def start_login(self) -> None:
        with self._lock:
            enabled = self.secure_auth_enabled
            auth_start_url = self.auth_start_url
        if not enabled:
            self.events.put({
                "kind": "auth_error",
                "text": "TXITY has not enabled secure Discord sign-in in its server configuration.",
            })
            return
        self.close()

        manager = self
        state = secrets.token_urlsafe(32)

        class CallbackHandler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: Any) -> None:
                return

            def _is_loopback_request(self) -> bool:
                expected_host = f"127.0.0.1:{self.server.server_port}"
                return (
                    self.client_address[0] == "127.0.0.1"
                    and self.headers.get("Host", "") == expected_host
                )

            def _respond(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Pragma", "no-cache")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, status: int, value: dict[str, Any]) -> None:
                self._respond(
                    status,
                    json.dumps(value, separators=(",", ":")).encode("utf-8"),
                    "application/json; charset=utf-8",
                )

            def do_GET(self) -> None:
                if not self._is_loopback_request():
                    self._json(403, {"ok": False, "error": "Loopback access only."})
                    return
                parts = urlsplit(self.path)
                if parts.path != "/callback":
                    self._json(404, {"ok": False, "error": "Not found."})
                    return
                query_state = parse_qs(parts.query).get("state", [""])[0]
                if not manager._state_matches(query_state):
                    self._json(400, {"ok": False, "error": "This sign-in request is invalid or expired."})
                    return
                html, csp = _callback_page(state)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Pragma", "no-cache")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", csp)
                self.end_headers()
                self.wfile.write(html)

            def do_POST(self) -> None:
                if not self._is_loopback_request():
                    self._json(403, {"ok": False, "error": "Loopback access only."})
                    return
                expected_origin = f"http://127.0.0.1:{self.server.server_port}"
                request_origin = self.headers.get("Origin")
                if request_origin and request_origin != expected_origin:
                    self._json(403, {"ok": False, "error": "Invalid callback origin."})
                    return
                if urlsplit(self.path).path != "/complete":
                    self._json(404, {"ok": False, "error": "Not found."})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = 0
                if length <= 0 or length > MAX_CALLBACK_BODY:
                    self._json(413, {"ok": False, "error": "Invalid callback size."})
                    return
                try:
                    payload = json.loads(self.rfile.read(length))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._json(400, {"ok": False, "error": "Invalid callback data."})
                    return
                if not isinstance(payload, dict) or not manager._state_matches(payload.get("state")):
                    self._json(400, {"ok": False, "error": "This sign-in request is invalid or expired."})
                    return
                token = payload.get("token")
                user = user_from_txity_token(token) if isinstance(token, str) else None
                if user is None:
                    self._json(400, {"ok": False, "error": "TXITY returned an invalid or expired session."})
                    return
                if not manager._complete(state, token, user, payload.get("joined") is True):
                    self._json(400, {"ok": False, "error": "This sign-in request is no longer active."})
                    return
                self._json(200, {"ok": True})
                threading.Thread(
                    target=manager._stop_server_for_state,
                    args=(state,),
                    daemon=True,
                    name="txity-auth-callback-close",
                ).start()

        server = ThreadingHTTPServer(("127.0.0.1", 0), CallbackHandler)
        server.daemon_threads = True

        with self._lock:
            self._server = server
            self._state = state
            self._completed = False
            self._timeout = threading.Timer(
                CALLBACK_TIMEOUT_SECONDS,
                self._expire,
                args=(state,),
            )
            timeout = self._timeout

        threading.Thread(
            target=server.serve_forever,
            daemon=True,
            name="txity-auth-callback",
        ).start()
        timeout.start()

        callback_url = (
            f"http://127.0.0.1:{server.server_port}/callback?"
            + urlencode({"state": state})
        )
        login_url = auth_start_url + "?" + urlencode({"return": callback_url})

        self.events.put({
            "kind": "auth_browser",
            "text": "Discord sign-in opened in your default browser. Waiting for TXITY to return to TXITPY.",
        })
        try:
            opened = webbrowser.open(login_url, new=2)
        except Exception:
            opened = False
        if not opened:
            self.events.put({
                "kind": "auth_error",
                "text": "Could not open the system browser for Discord sign-in.",
            })
            self._stop_server_for_state(state)

    def _state_matches(self, value: Any) -> bool:
        with self._lock:
            return (
                isinstance(value, str)
                and bool(self._state)
                and not self._completed
                and hmac.compare_digest(value, self._state)
            )

    def _complete(
        self,
        state: str,
        token: str,
        user: dict[str, str],
        joined: bool,
    ) -> bool:
        with self._lock:
            if not self._state or self._completed or not hmac.compare_digest(state, self._state):
                return False
            self._completed = True
            timeout = self._timeout
            self._timeout = None
        if timeout is not None:
            timeout.cancel()
        self.events.put({
            "kind": "auth_token",
            "token": token,
            "user": user,
            "joined": joined,
        })
        return True

    def _expire(self, state: str) -> None:
        if not self._state_matches(state):
            return
        self._stop_server_for_state(state)
        self.events.put({
            "kind": "auth_error",
            "text": "Discord sign-in timed out. Start the sign-in flow again.",
        })

    def _stop_server_for_state(self, state: str) -> None:
        with self._lock:
            if not self._state or not hmac.compare_digest(state, self._state):
                return
            server = self._server
            timeout = self._timeout
            self._server = None
            self._timeout = None
            self._state = ""
        if timeout is not None:
            timeout.cancel()
        if server is not None:
            server.shutdown()
            server.server_close()

    def close(self) -> None:
        with self._lock:
            state = self._state
        if state:
            self._stop_server_for_state(state)
