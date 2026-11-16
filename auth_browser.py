"""Embedded TXITY browser for the site's official Discord login flow."""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import webview

from discord_auth import clean_discord_user, is_auth_token

BASE_URL = "https://txity.radiodiscussion.org"
ALLOWED_HOST = "txity.radiodiscussion.org"

AUTH_BRIDGE_SCRIPT = r"""
(() => {
    "use strict";
    if (window.__txitypyAuthBridgeInstalled) return;
    window.__txitypyAuthBridgeInstalled = true;

    const TOKEN_KEY = "txity.discordAuthToken.v1";
    const USER_KEY = "txity.discordUser.v1";
    let lastReported = null;

    function readUser() {
        try {
            const raw = window.localStorage.getItem(USER_KEY);
            const user = raw ? JSON.parse(raw) : null;
            if (!user || typeof user !== "object" || Array.isArray(user)) return null;
            if (typeof user.id !== "string" || typeof user.username !== "string") return null;
            return {
                id: user.id,
                username: user.username,
                globalName: typeof user.globalName === "string" ? user.globalName : null,
                avatarUrl: typeof user.avatarUrl === "string" ? user.avatarUrl : null
            };
        } catch (_) {
            return null;
        }
    }

    function reportState() {
        if (location.origin !== "https://txity.radiodiscussion.org") return;

        let token = "";
        try {
            token = window.localStorage.getItem(TOKEN_KEY) || "";
        } catch (_) {
            return;
        }

        const user = token ? readUser() : null;
        const signature = token + "\n" + JSON.stringify(user);
        const api = window.pywebview && window.pywebview.api;
        if (!api || typeof api.report_auth_state !== "function") return;
        if (signature === lastReported) return;

        lastReported = signature;
        api.report_auth_state({ origin: location.origin, token, user }).catch(() => {
            lastReported = null;
        });
    }

    reportState();
    window.setInterval(reportState, 500);
})();
"""


class AuthBridge:
    """Receive only TXITY's exact localStorage keys from its HTTPS origin."""

    def __init__(self) -> None:
        self.window: Any = None
        self._last_token = ""
        self._lock = threading.Lock()

    def _on_txity_origin(self) -> bool:
        if self.window is None:
            return False
        try:
            current = urlsplit(self.window.get_current_url() or "")
        except Exception:
            return False
        return (
            current.scheme == "https"
            and current.hostname == ALLOWED_HOST
            and current.port in (None, 443)
        )

    def report_auth_state(self, payload: Any) -> bool:
        if not isinstance(payload, dict) or not self._on_txity_origin():
            return False
        if payload.get("origin") != BASE_URL:
            return False

        token = payload.get("token")
        if token:
            if not is_auth_token(token):
                return False
            user = clean_discord_user(payload.get("user"))
            with self._lock:
                changed = token != self._last_token
                self._last_token = token
            if changed:
                emit({"type": "auth_token", "token": token, "user": user})
            return True

        with self._lock:
            had_token = bool(self._last_token)
            self._last_token = ""
        if had_token:
            emit({"type": "signed_out"})
        return True

    def logout(self) -> bool:
        """Invoke TXITY's own logout control instead of editing its storage."""
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self._on_txity_origin():
                try:
                    result = self.window.evaluate_js(
                        "(() => {"
                        "const button = document.getElementById('discordLogout');"
                        "if (!button || button.hidden || button.disabled) return false;"
                        "button.click(); return true;"
                        "})()"
                    )
                    return result is True
                except Exception:
                    return False
            time.sleep(0.25)
        return False


_OUTPUT_LOCK = threading.Lock()


def emit(message: dict[str, Any]) -> None:
    with _OUTPUT_LOCK:
        sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
        sys.stdout.flush()


def read_commands(bridge: AuthBridge) -> None:
    for line in sys.stdin:
        try:
            command = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(command, dict) and command.get("command") == "logout":
            if not bridge.logout():
                emit({
                    "type": "browser_error",
                    "text": "No active TXITY Discord session was available to sign out.",
                })


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--storage-path", required=True)
    args = parser.parse_args()

    bridge = AuthBridge()
    window = webview.create_window(
        "TXITY | Discord sign-in",
        BASE_URL,
        js_api=bridge,
        width=1180,
        height=820,
        min_size=(760, 560),
    )
    bridge.window = window

    def on_loaded(*_args: Any) -> None:
        if not bridge._on_txity_origin():
            return
        try:
            window.evaluate_js(AUTH_BRIDGE_SCRIPT)
        except Exception:
            return

    window.events.loaded += on_loaded
    window.events.closed += lambda *_args: emit({"type": "closed"})
    threading.Thread(
        target=read_commands,
        args=(bridge,),
        daemon=True,
        name="txity-auth-commands",
    ).start()

    try:
        webview.start(
            private_mode=False,
            storage_path=args.storage_path,
            debug=False,
        )
    except Exception:
        emit({"type": "browser_error", "text": "Could not start the embedded TXITY browser."})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
