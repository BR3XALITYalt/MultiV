"""Embedded TXITY sign-in window.

The TXITY server returns its signed session to its own homepage fragment. The
website then stores that session in its same-origin localStorage. This helper
uses a dedicated WebView2 profile, reads only TXITY's own storage, and sends
the token to the parent desktop process over its one-use loopback callback.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from discord_auth import user_from_txity_token

TXITY_ORIGIN = "https://txity.radiodiscussion.org"

WATCHER_SCRIPT = r"""
(() => {
    if (window.__txitpyAuthWatcher) {
        clearInterval(window.__txitpyAuthWatcher);
    }

    window.__txitpyAuthWatcher = setInterval(() => {
        if (location.origin !== "https://txity.radiodiscussion.org") {
            return;
        }

        let token = "";
        try {
            token = localStorage.getItem("txity.discordAuthToken.v1") || "";
        } catch (_) {
            return;
        }

        const api = window.pywebview && window.pywebview.api;
        if (!token || !api || typeof api.complete !== "function") {
            return;
        }

        clearInterval(window.__txitpyAuthWatcher);
        api.complete(location.origin, token).then(result => {
            if (result && result.ok) {
                document.title = "Discord sign-in complete";
                document.body.innerHTML =
                    "<main style='font:16px system-ui;max-width:36rem;margin:12vh auto;padding:0 1.5rem;color:#171717'>" +
                    "<h1>Sign-in complete</h1><p>You can return to TXITPY.</p></main>";
            } else {
                document.title = "TXITPY sign-in could not be completed";
            }
        }).catch(() => {
            document.title = "TXITPY sign-in could not be completed";
        });
    }, 300);
})();
"""



def diagnostic_log_path() -> Path:
    """Return the per-user location for sign-in diagnostics."""
    root = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    return root / "TXITPY" / "logs" / "discord-auth.log"


def safe_error_text(error: BaseException) -> str:
    """Remove URLs, token-like strings, and control characters from errors."""
    message = str(error).replace("\r", " ").replace("\n", " ")
    message = re.sub(r"https?://\S+", "[URL redacted]", message)
    message = re.sub(
        r"(?i)\b(token|authToken|code|state|secret)=([^&\s]+)",
        r"\1=[redacted]",
        message,
    )
    message = re.sub(r"\b[A-Za-z0-9_-]{48,}\b", "[value redacted]", message)
    message = "".join(char for char in message if char.isprintable())
    return message[:500] or "No additional error details were provided."


def write_diagnostic(stage: str, error: BaseException) -> Path | None:
    """Write a local sign-in diagnostic without recording login credentials."""
    path = diagnostic_log_path()
    detail = safe_error_text(error)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(
                f"{datetime.now(timezone.utc).isoformat()} "
                f"[{stage}] {type(error).__name__}: {detail}\n"
            )
        return path
    except OSError:
        return None

def token_from_storage(origin: str, token: str) -> str | None:
    """Accept a token only when read from TXITY's exact origin and still valid."""
    if origin != TXITY_ORIGIN or user_from_txity_token(token) is None:
        return None
    return token


class AuthBridge:
    """Validate and forward a token from the embedded TXITY page."""

    def __init__(self, callback_url: str, state: str) -> None:
        self.callback_url = callback_url
        self.failure_url = callback_url.rsplit("/", 1)[0] + "/failure"
        self.state = state
        self._lock = threading.Lock()
        self._finished = False
        self.window = None

    def complete(self, origin: str, token: str) -> dict[str, bool]:
        """Forward a valid token read from TXITY's own origin."""
        if token_from_storage(origin, token) is None:
            return {"ok": False}

        with self._lock:
            if self._finished:
                return {"ok": False}
            self._finished = True

        payload = json.dumps({
            "state": self.state,
            "token": token,
            "joined": False,
        }).encode("utf-8")
        request = Request(
            self.callback_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=8) as response:
                result = json.loads(response.read().decode("utf-8"))
            if not isinstance(result, dict) or result.get("ok") is not True:
                return {"ok": False}
        except (HTTPError, URLError, TimeoutError, OSError, ValueError):
            return {"ok": False}

        window = self.window
        if window is not None:
            try:
                window.destroy()
            except Exception:
                pass
        return {"ok": True}

    def report_startup_failure(self, stage: str, error: BaseException) -> None:
        """Report the actual browser startup failure without exposing credentials."""
        detail = safe_error_text(error)
        log_path = write_diagnostic(stage, error)
        if stage == "import":
            message = (
                "TXITPY could not load the embedded browser package. "
                "Run: python -m pip install -r requirements.txt. "
                f"Details: {type(error).__name__}: {detail}"
            )
        else:
            message = (
                "TXITPY could not start its embedded browser. "
                f"Details: {type(error).__name__}: {detail}"
            )
            lowered = detail.lower()
            if any(word in lowered for word in ("webview2", "edgechromium", "runtime not found")):
                message += (
                    " Install the Microsoft Edge WebView2 Runtime from "
                    "https://developer.microsoft.com/microsoft-edge/webview2/."
                )
        if log_path is not None:
            message += f" Diagnostic log: {log_path}"
        payload = json.dumps({
            "state": self.state,
            "message": message[:1800],
        }).encode("utf-8")
        request = Request(
            self.failure_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=5):
                pass
        except (HTTPError, URLError, TimeoutError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 3:
        return 2

    login_url, callback_url, state = args
    bridge = AuthBridge(callback_url, state)

    try:
        import webview
    except Exception as exc:
        bridge.report_startup_failure("import", exc)
        return 1

    try:
        window = webview.create_window(
            "Sign in to TXITY",
            url=login_url,
            js_api=bridge,
            width=920,
            height=760,
            min_size=(640, 560),
            background_color="#000000",
        )
    except Exception as exc:
        bridge.report_startup_failure("create-window", exc)
        return 1

    bridge.window = window

    def on_loaded(loaded_window) -> None:
        try:
            loaded_window.run_js(WATCHER_SCRIPT)
        except Exception:
            # A page may navigate while the script is being installed.
            # The next loaded event retries the same-origin storage watcher.
            pass

    window.events.loaded += on_loaded

    local_app_data = os.environ.get("LOCALAPPDATA")
    profile_root = Path(local_app_data) if local_app_data else Path.home()
    profile_path = profile_root / "TXITPY" / "BrowserProfile"
    profile_path.mkdir(parents=True, exist_ok=True)

    try:
        webview.start(
            private_mode=False,
            storage_path=str(profile_path),
            debug=False,
        )
    except Exception as exc:
        bridge.report_startup_failure("start", exc)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
