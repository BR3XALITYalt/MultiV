"""Embedded TXITY sign-in window.

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
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from discord_auth import (
    format_exception_details,
    safe_error_text,
    user_from_txity_token,
    write_diagnostic,
    write_diagnostic_text,
)

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
    }, 1000);
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
        self._window = None

    def _send_failure(self, message: str) -> None:
        """Send a sanitized browser failure to the parent desktop process."""
        payload = json.dumps({
            "state": self.state,
            "message": safe_error_text(message, max_chars=14_000),
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
        except Exception as exc:
            write_diagnostic("send-failure-to-parent", exc)

    def complete(self, origin: str, token: str) -> dict[str, bool]:
        """Forward a valid token read from TXITY's own origin."""
        if token_from_storage(origin, token) is None:
            error = ValueError(
                "The embedded page did not provide a valid, unexpired TXITY session "
                "from the expected HTTPS origin."
            )
            details = format_exception_details(error)
            log_path = write_diagnostic("session-validation", error)
            message = (
                "Discord sign-in could not validate the returned session.\n\n"
                f"{details}"
            )
            if log_path is not None:
                message += f"\n\nDiagnostic log: {log_path}"
            self._send_failure(message)
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
                raise RuntimeError("The local sign-in callback returned an invalid response.")
        except Exception as exc:
            details = format_exception_details(exc)
            log_path = write_diagnostic("complete-local-callback", exc)
            message = (
                "TXITPY could not transfer the Discord session to the desktop application.\n\n"
                f"{details}"
            )
            if log_path is not None:
                message += f"\n\nDiagnostic log: {log_path}"
            self._send_failure(message)
            return {"ok": False}

        window = self._window
        if window is not None:
            try:
                window.destroy()
            except Exception as exc:
                write_diagnostic("close-sign-in-window", exc)
        return {"ok": True}

    def report_startup_failure(self, stage: str, error: BaseException) -> None:
        """Report the browser startup traceback without exposing credentials."""
        details = format_exception_details(error)
        log_path = write_diagnostic(stage, error)
        message = (
            f"Embedded browser failure during stage: {stage}.\n"
            f"Exception: {type(error).__name__}: {safe_error_text(error)}\n\n"
            f"Full traceback:\n{details}"
        )
        if stage == "import-webview":
            message += (
                "\n\nInstall the TXITPY Python dependencies with: "
                "python -m pip install -r requirements.txt"
            )
        lowered = details.lower()
        if any(word in lowered for word in ("webview2", "edgechromium", "runtime not found")):
            message += (
                "\n\nInstall the Microsoft Edge WebView2 Runtime from "
                "https://developer.microsoft.com/microsoft-edge/webview2/."
            )
        if log_path is not None:
            message += f"\n\nDiagnostic log: {log_path}"
        self._send_failure(message)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 3:
        return 2

    def report_stage(message: str) -> None:
        print(f"[TXITPY] {message}", file=sys.stderr, flush=True)

    report_stage("Embedded browser helper process started.")
    os.environ["PYWEBVIEW_LOG"] = "debug"
    login_url, callback_url, state = args
    bridge = AuthBridge(callback_url, state)

    report_stage("Importing pywebview.")
    try:
        import webview
    except Exception as exc:
        bridge.report_startup_failure("import-webview", exc)
        return 1

    try:
        from importlib.metadata import version
        installed_version = version("pywebview")
    except Exception:
        installed_version = "unknown"
    report_stage(f"pywebview {installed_version} imported successfully.")

    local_app_data = os.environ.get("LOCALAPPDATA")
    profile_root = Path(local_app_data) if local_app_data else Path.home()
    profile_path = profile_root / "TXITPY" / "BrowserProfile"
    report_stage(f"Preparing browser profile at {profile_path}.")
    try:
        profile_path.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        bridge.report_startup_failure("create-browser-profile", exc)
        return 1
    report_stage("Browser profile is ready.")

    report_stage("Creating the Discord sign-in browser window.")
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

    bridge._window = window
    report_stage("Browser window object created.")

    window_shown = threading.Event()
    page_loaded = threading.Event()

    def on_initialized(renderer_name) -> None:
        report_stage(f"pywebview initialized renderer: {renderer_name}.")

    def on_shown() -> None:
        window_shown.set()
        report_stage("Embedded sign-in window is visible; waiting for Discord login to load.")

    def on_loaded(loaded_window=None) -> None:
        page_loaded.set()
        page_window = loaded_window if loaded_window is not None else window
        try:
            current_url = page_window.get_current_url() or ""
            parsed_url = urlsplit(current_url)
            page_location = parsed_url.netloc + parsed_url.path
            print(
                f"[TXITPY] Sign-in page loaded: {page_location or 'URL unavailable'}",
                file=sys.stderr,
                flush=True,
            )
            write_diagnostic_text("page-loaded", page_location or "URL unavailable")
            page_window.run_js(WATCHER_SCRIPT)
        except Exception as exc:
            # Navigation can race script injection, so keep the window open
            # and report the underlying browser error.
            details = format_exception_details(exc)
            print(
                f"[TXITPY browser page-load error]\n{details}",
                file=sys.stderr,
                flush=True,
            )
            write_diagnostic("inject-auth-watcher", exc)

    try:
        window.events.initialized += on_initialized
        window.events.shown += on_shown
        window.events.loaded += on_loaded
    except Exception as exc:
        bridge.report_startup_failure("register-window-events", exc)
        return 1

    def report_browser_stall() -> None:
        if not window_shown.wait(20):
            message = (
                "The browser process started, but pywebview did not show its window "
                "within 20 seconds. The GUI renderer may be blocked during startup."
            )
            print(f"[TXITPY browser warning] {message}", file=sys.stderr, flush=True)
            write_diagnostic_text("window-show-timeout", message)
            return
        if not page_loaded.wait(30):
            message = (
                "The sign-in window appeared, but the TXITY webpage did not finish "
                "loading within 30 seconds."
            )
            print(f"[TXITPY browser warning] {message}", file=sys.stderr, flush=True)
            write_diagnostic_text("page-load-timeout", message)

    threading.Thread(
        target=report_browser_stall,
        daemon=True,
        name="txity-browser-stall-watchdog",
    ).start()

    try:
        renderer = "edgechromium" if sys.platform == "win32" else None
        try:
            from importlib.metadata import version
            webview_version = version("pywebview")
        except Exception:
            webview_version = "unknown"

        report_stage(
            f"Calling webview.start with pywebview {webview_version}; "
            f"renderer={renderer or 'platform default'}."
        )
        webview.start(
            gui=renderer,
            private_mode=False,
            storage_path=str(profile_path),
            debug=False,
        )
    except Exception as exc:
        bridge.report_startup_failure("start-webview", exc)
        return 1

    report_stage("pywebview event loop exited normally.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
