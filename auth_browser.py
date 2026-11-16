"""Embedded browser for TXITY's own Discord sign-in flow.

The browser runs TXITY's official website and JavaScript. This helper does not
perform OAuth itself and never writes the authenticated token to disk.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from urllib.parse import urlsplit
from typing import Any

BASE_URL = "https://txity.radiodiscussion.org"
ALLOWED_HOST = "txity.radiodiscussion.org"

SCAN_SCRIPT = r"""
(() => {
    if (window.__txitypyAuthScanInstalled) return;
    window.__txitypyAuthScanInstalled = true;

    const tokenPattern = /auth.?token|permission.?token|discord.*access.?token|txity.*token/i;
    const safeToken = value =>
        typeof value === "string" &&
        value.length >= 16 &&
        value.length <= 8192 &&
        !/[\s\u0000-\u001f]/.test(value);

    function scan() {
        if (location.origin !== "https://txity.radiodiscussion.org") return;

        const candidates = [];
        const add = (key, value, source) => {
            if (safeToken(value)) candidates.push({ key, value, source });
        };

        function walk(value, path, depth) {
            if (!value || typeof value !== "object" || depth > 5) return;
            for (const [key, nested] of Object.entries(value)) {
                const name = path ? path + "." + key : key;
                if (tokenPattern.test(key) && typeof nested === "string") {
                    add(name, nested, "storage-json");
                } else if (nested && typeof nested === "object") {
                    walk(nested, name, depth + 1);
                }
            }
        }

        for (const storageName of ["localStorage", "sessionStorage"]) {
            let storage;
            try { storage = window[storageName]; } catch (_) { continue; }
            if (!storage) continue;

            for (let index = 0; index < storage.length; index++) {
                const key = storage.key(index);
                let value;
                try { value = storage.getItem(key); } catch (_) { continue; }
                if (tokenPattern.test(key)) add(key, value, storageName);
                try { walk(JSON.parse(value), key, 0); } catch (_) {}
                if (typeof value === "string" && value.split(".").length === 3 &&
                    /^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(value)) {
                    add(key, value, "jwt-storage");
                }
            }
        }

        try {
            for (const item of performance.getEntriesByType("resource")) {
                if (!item.name.includes("/ws/control")) continue;
                const resource = new URL(item.name);
                if (resource.hostname !== "txity.radiodiscussion.org") continue;
                add("authToken", resource.searchParams.get("authToken"), "control-websocket");
            }
        } catch (_) {}

        try {
            for (const cookie of document.cookie.split(";")) {
                const separator = cookie.indexOf("=");
                if (separator < 0) continue;
                const key = cookie.slice(0, separator).trim();
                const value = cookie.slice(separator + 1).trim();
                if (tokenPattern.test(key)) add(key, decodeURIComponent(value), "cookie");
            }
        } catch (_) {}

        // Prefer the exact authToken name, then other explicit TXITY auth keys.
        candidates.sort((left, right) => {
            const rank = item =>
                item.key.toLowerCase() === "authtoken" ? 0 :
                /auth.?token/i.test(item.key) ? 1 :
                /permission.?token/i.test(item.key) ? 2 : 3;
            return rank(left) - rank(right);
        });

        const selected = candidates[0] || null;
        const state = {
            origin: location.origin,
            signedIn: Boolean(selected),
            token: selected ? selected.value : null,
            source: selected ? selected.source : null,
            key: selected ? selected.key : null
        };

        const api = window.pywebview && window.pywebview.api;
        if (api && typeof api.report_auth_state === "function") {
            api.report_auth_state(state).catch(() => {});
        }
    }

    scan();
    window.setInterval(scan, 1800);
})();
"""


class AuthBridge:
    def __init__(self) -> None:
        self.window: Any = None
        self._last_token: str | None = None
        self._lock = threading.Lock()

    def report_auth_state(self, payload: Any) -> bool:
        """Accept a token only while the embedded browser is on TXITY's origin."""
        if not isinstance(payload, dict) or self.window is None:
            return False

        try:
            current = urlsplit(self.window.get_current_url() or "")
        except Exception:
            return False

        if current.scheme != "https" or current.hostname != ALLOWED_HOST:
            return False
        if payload.get("origin") != BASE_URL:
            return False

        token = payload.get("token")
        signed_in = payload.get("signedIn") is True

        if signed_in and isinstance(token, str):
            if not 16 <= len(token) <= 8192:
                return False
            if any(character.isspace() or ord(character) < 32 for character in token):
                return False

            with self._lock:
                changed = token != self._last_token
                self._last_token = token

            if changed:
                emit({"type": "auth_token", "token": token})
            return True

        with self._lock:
            had_token = self._last_token is not None
            self._last_token = None

        if had_token:
            emit({"type": "signed_out"})
        return True


_OUTPUT_LOCK = threading.Lock()


def emit(message: dict[str, Any]) -> None:
    """Send one private IPC message to the parent process."""
    with _OUTPUT_LOCK:
        sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
        sys.stdout.flush()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--storage-path", required=True)
    args = parser.parse_args()

    try:
        import webview
    except ImportError:
        emit({
            "type": "browser_error",
            "text": "pywebview is not installed. Install the project requirements.",
        })
        return 2

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
        try:
            current = urlsplit(window.get_current_url() or "")
            if current.scheme == "https" and current.hostname == ALLOWED_HOST:
                window.evaluate_js(SCAN_SCRIPT)
        except Exception:
            # Navigation may briefly invalidate the document while Discord
            # redirects back to TXITY. The next loaded event retries.
            return

    window.events.loaded += on_loaded
    window.events.closed += lambda *_args: emit({"type": "closed"})

    try:
        webview.start(
            private_mode=False,
            storage_path=args.storage_path,
            debug=False,
        )
    except Exception as exc:
        emit({"type": "browser_error", "text": f"Could not start the embedded browser: {exc}"})
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
