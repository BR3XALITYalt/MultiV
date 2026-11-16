"""Discord sign-in handoff for TXITY through an embedded browser window.

TXITY performs Discord OAuth and stores its signed session in its own
same-origin browser storage. An embedded WebView reads that value and sends it
to this process through a one-use loopback callback.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Queue
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

BASE_URL = "https://txity.radiodiscussion.org"
AUTH_START_URL = f"{BASE_URL}/auth/discord/start"
CALLBACK_TIMEOUT_SECONDS = 300
MAX_CALLBACK_BODY = 20_000


def diagnostic_log_path(filename: str = "discord-auth.log") -> Path:
    """Return the per-user location for diagnostic logs."""
    root = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    return root / "TXITPY" / "logs" / filename


def safe_error_text(value: BaseException | str, max_chars: int = 16_000) -> str:
    """Preserve exception context while removing URLs and credential values."""
    message = str(value).replace("\r", "")
    message = re.sub(r"https?://[^\s'\"<>]+", "[URL redacted]", message)
    message = re.sub(
        r"""(?i)(["']?(?:access[_-]?token|refresh[_-]?token|discord[_-]?auth|authtoken|token|code|state|secret|sessionsecret)["']?\s*[:=]\s*)["']?([^&\s,"'}]+)["']?""",
        r"\1[redacted]",
        message,
    )
    message = re.sub(r"(?i)\bBearer\s+[^\s,]+", "Bearer [redacted]", message)
    message = re.sub(r"\b[A-Za-z0-9_-]{48,}\b", "[long value redacted]", message)
    message = "".join(
        character for character in message
        if character in "\n\t" or character.isprintable()
    )
    return message[:max_chars] or "No additional error details were provided."


def format_exception_details(error: BaseException, max_chars: int = 16_000) -> str:
    """Format a complete traceback and sanitize secrets before displaying it."""
    details = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    return safe_error_text(details, max_chars=max_chars)


def write_diagnostic_text(
    stage: str,
    details: str,
    filename: str = "discord-auth.log",
) -> Path | None:
    """Append sanitized diagnostic details to a local log."""
    path = diagnostic_log_path(filename)
    clean_details = safe_error_text(details, max_chars=24_000)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(
                f"{datetime.now(timezone.utc).isoformat()} [{stage}]\n"
                f"{clean_details}\n\n"
            )
        return path
    except OSError:
        return None


def write_diagnostic(
    stage: str,
    error: BaseException,
    filename: str = "discord-auth.log",
) -> Path | None:
    """Append a sanitized exception and its full traceback to the local log."""
    return write_diagnostic_text(
        stage,
        format_exception_details(error),
        filename=filename,
    )


def read_diagnostic_since(
    offset: int = 0,
    filename: str = "discord-auth.log",
    max_chars: int = 14_000,
) -> str:
    """Read diagnostic entries written after a browser process was started."""
    path = diagnostic_log_path(filename)
    try:
        with path.open("rb") as stream:
            stream.seek(max(0, offset))
            return safe_error_text(
                stream.read().decode("utf-8", errors="replace"),
                max_chars=max_chars,
            )
    except OSError:
        return ""


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
    """Open TXITY in an embedded browser and receive its signed token locally."""

    def __init__(self, events: Queue[dict[str, Any]]) -> None:
        self.events = events
        self._lock = threading.RLock()
        self._server: ThreadingHTTPServer | None = None
        self._state = ""
        self._completed = False
        self._timeout: threading.Timer | None = None
        self._browser_process: subprocess.Popen[Any] | None = None
        self._restore_only = False
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

    def restore_session(self) -> None:
        """Check the existing WebView profile for a valid saved TXITY session."""
        self.start_login(restore_only=True)

    def start_login(self, restore_only: bool = False) -> None:
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
        with self._lock:
            self._restore_only = restore_only

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
                path = urlsplit(self.path).path
                if path not in {"/complete", "/failure"}:
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

                if path == "/failure":
                    raw_message = payload.get("message")
                    if isinstance(raw_message, str):
                        message = safe_error_text(raw_message, max_chars=16_000)
                    else:
                        message = "The embedded sign-in window could not start."
                    if payload.get("code") == "no-stored-session":
                        accepted = manager._restore_missing(state)
                    else:
                        accepted = manager._fail(state, message)
                    if not accepted:
                        self._json(400, {"ok": False, "error": "This sign-in request is no longer active."})
                        return
                    self._json(200, {"ok": True})
                    threading.Thread(
                        target=manager._stop_server_for_state,
                        args=(state,),
                        daemon=True,
                        name="txity-auth-failure-close",
                    ).start()
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
        login_url = (
            f"{BASE_URL}/"
            if restore_only
            else auth_start_url + "?" + urlencode({"return": callback_url})
        )

        complete_url = f"http://127.0.0.1:{server.server_port}/complete"
        helper_path = Path(__file__).with_name("discord_auth_browser.py")
        log_path = diagnostic_log_path()
        try:
            diagnostic_offset = log_path.stat().st_size
        except OSError:
            diagnostic_offset = 0
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-u",
                    str(helper_path),
                    login_url,
                    complete_url,
                    state,
                    "restore" if restore_only else "login",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creation_flags,
            )
        except OSError as exc:
            self._stop_server_for_state(state)
            details = format_exception_details(exc)
            saved_log = write_diagnostic("launch-browser-process", exc)
            message = (
                "TXITPY could not launch the sign-in browser process.\n\n"
                f"{details}"
            )
            if saved_log is not None:
                message += f"\n\nDiagnostic log: {saved_log}"
            self.events.put({"kind": "auth_error", "text": message})
            return

        with self._lock:
            self._browser_process = process
        output_reader = None
        if process.stdout is not None:
            output_reader = threading.Thread(
                target=self._capture_browser_output,
                args=(process.stdout,),
                daemon=True,
                name="txity-browser-output",
            )
            output_reader.start()
        self.events.put({
            "kind": "auth_browser",
            "text": (
                "Checking for a saved Discord session."
                if restore_only
                else "TXITY sign-in opened in the embedded browser. Waiting for Discord authorization."
            ),
        })
        threading.Thread(
            target=self._watch_browser,
            args=(process, state, diagnostic_offset, output_reader),
            daemon=True,
            name="txity-auth-browser-monitor",
        ).start()

    def _capture_browser_output(self, stream: Any) -> None:
        """Print explicit startup checkpoints immediately and retain error context."""
        path = diagnostic_log_path()
        pending: list[str] = []
        context_remaining = 0

        def flush_problem_block(log_stream: Any) -> None:
            if not pending:
                return
            block = "\n".join(pending)
            timestamp = datetime.now(timezone.utc).isoformat()
            log_stream.write(f"{timestamp} [browser-output]\n{block}\n\n")
            log_stream.flush()
            print(
                f"\n[TXITPY embedded browser]\n{block}\n",
                file=sys.stderr,
                flush=True,
            )
            pending.clear()

        def write_checkpoint(log_stream: Any, detail: str) -> None:
            timestamp = datetime.now(timezone.utc).isoformat()
            log_stream.write(f"{timestamp} [browser-checkpoint] {detail}\n")
            log_stream.flush()
            print(detail, file=sys.stderr, flush=True)

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as log_stream:
                for line in stream:
                    detail = safe_error_text(line.rstrip("\r\n"), max_chars=3_000)
                    if not detail:
                        continue

                    if detail.startswith("[TXITPY]"):
                        flush_problem_block(log_stream)
                        write_checkpoint(log_stream, detail)
                        continue

                    lowered = detail.lower()
                    is_problem = any(
                        marker in lowered
                        for marker in (
                            "error",
                            "exception",
                            "traceback",
                            "failed",
                            "failure",
                            "fatal",
                            "warning",
                            "could not",
                            "unable to",
                            "not found",
                        )
                    )

                    if is_problem:
                        flush_problem_block(log_stream)
                        pending.append(detail)
                        context_remaining = 20
                        # Surface the first error line immediately. Additional
                        # traceback context is grouped into the same log block.
                        print(
                            f"[TXITPY embedded browser] {detail}",
                            file=sys.stderr,
                            flush=True,
                        )
                    elif context_remaining > 0:
                        pending.append(detail)
                        context_remaining -= 1
                        if context_remaining == 0:
                            flush_problem_block(log_stream)

                flush_problem_block(log_stream)
        except Exception as exc:
            details = format_exception_details(exc)
            print(
                f"\n[TXITPY browser diagnostic reader error]\n{details}\n",
                file=sys.stderr,
                flush=True,
            )
            write_diagnostic("capture-browser-output", exc)
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _watch_browser(
        self,
        process: subprocess.Popen[Any],
        state: str,
        diagnostic_offset: int,
        output_reader: threading.Thread | None,
    ) -> None:
        exit_code = process.wait()
        if output_reader is not None:
            output_reader.join(timeout=2)
        with self._lock:
            if self._browser_process is process:
                self._browser_process = None
            active = (
                bool(self._state)
                and not self._completed
                and hmac.compare_digest(state, self._state)
            )
        if active:
            self._stop_server_for_state(state)
            if exit_code == 0:
                message = (
                    "The sign-in window was closed before Discord sign-in completed. "
                    "Process exit code: 0."
                )
            else:
                details = read_diagnostic_since(diagnostic_offset)
                message = (
                    "The embedded sign-in window exited unexpectedly. "
                    f"Process exit code: {exit_code}."
                )
                if details:
                    message += f"\n\nBrowser diagnostics:\n{details}"
                else:
                    message += (
                        "\n\nThe browser process did not write a diagnostic traceback. "
                        "Check the WebView2 Runtime installation and the Python environment."
                    )
                message += f"\n\nDiagnostic log: {diagnostic_log_path()}"
            self.events.put({"kind": "auth_error", "text": message})

    def _restore_missing(self, state: str) -> bool:
        """Finish a silent restore attempt when no usable session is stored."""
        with self._lock:
            if (
                not self._restore_only
                or not self._state
                or self._completed
                or not hmac.compare_digest(state, self._state)
            ):
                return False
            self._completed = True
            timeout = self._timeout
            self._timeout = None
        if timeout is not None:
            timeout.cancel()
        self.events.put({"kind": "auth_restore_missing"})
        return True

    def _fail(self, state: str, message: str) -> bool:
        with self._lock:
            if not self._state or self._completed or not hmac.compare_digest(state, self._state):
                return False
            self._completed = True
            timeout = self._timeout
            self._timeout = None
        if timeout is not None:
            timeout.cancel()
        self.events.put({"kind": "auth_error", "text": message})
        return True

    def _terminate_browser_process(self) -> None:
        with self._lock:
            process = self._browser_process
            self._browser_process = None
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
            except OSError:
                pass

    def clear_browser_session(self) -> bool:
        """Remove the embedded browser profile used for TXITY sign-in."""
        self.close()
        local_app_data = os.environ.get("LOCALAPPDATA")
        profile_root = Path(local_app_data) if local_app_data else Path.home()
        profile_path = profile_root / "TXITPY" / "BrowserProfile"
        try:
            shutil.rmtree(profile_path, ignore_errors=False)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return not profile_path.exists()

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
        self._terminate_browser_process()
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
            self._restore_only = False
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
        self._terminate_browser_process()
