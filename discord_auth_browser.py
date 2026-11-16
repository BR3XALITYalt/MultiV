""""Embedded TXITY sign-in window.

The TXITY server returns its signed session to its own homepage fragment. The
website then stores that session in its same-origin localStorage. This helper
uses a dedicated WebView2 profile, reads only TXITY's own storage, and sends
the token to the parent desktop process over its one-use loopback callback.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from discord_auth import user_from_txity_token

TXITY_ORIGIN = "https://txity.radiodiscussion.org"
TOKEN_STORAGE_KEY = "txity.discordAuthToken.v1"
CALLBACK_TIMEOUT_SECONDS = 300

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

    def report_startup_failure(self) -> None:
        """Tell TXITPY that the embedded browser could not be started."""
        payload = json.dumps({
            "state": self.state,
            "message": (
                "The embedded sign-in window could not start. Install the "
                "Microsoft Edge WebView2 Runtime, then restart TXITPY."
            ),
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
    except Exception:
        bridge.report_startup_failure()
        return 1

    window = webview.create_window(
        "Sign in to TXITY",
        url=login_url,
        width=920,
        height=760,
        min_size=(640, 560),
        background_color="#000000",
    )
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
    except Exception:
        bridge.report_startup_failure()
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
