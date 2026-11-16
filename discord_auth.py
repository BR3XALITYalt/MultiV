"""Launch and manage the embedded browser used for TXITY Discord sign-in."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from queue import Queue
from typing import Any


def is_auth_token(value: Any) -> bool:
    """Validate an opaque TXITY token without decoding or logging it."""
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


def _storage_directory() -> Path:
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return root / "TXITPY" / "browser-profile"


class DiscordAuthBrowser:
    """Run TXITY itself so its OAuth redirects and account code remain authoritative."""

    def __init__(self, events: Queue[dict[str, Any]]) -> None:
        self.events = events
        self.process: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._reader: threading.Thread | None = None

    def open(self) -> None:
        with self._lock:
            if self.process is not None and self.process.poll() is None:
                self.events.put({
                    "kind": "auth_browser",
                    "text": "TXITY's sign-in browser is already open.",
                })
                return

            helper = Path(__file__).with_name("auth_browser.py")
            if not helper.is_file():
                self.events.put({"kind": "auth_error", "text": "The TXITY browser helper is missing."})
                return

            storage_path = _storage_directory()
            try:
                storage_path.mkdir(parents=True, exist_ok=True)
                self.process = subprocess.Popen(
                    [sys.executable, str(helper), "--storage-path", str(storage_path)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except OSError:
                self.process = None
                self.events.put({"kind": "auth_error", "text": "Could not open the TXITY sign-in browser."})
                return

            self._reader = threading.Thread(
                target=self._read_messages,
                args=(self.process,),
                daemon=True,
                name="txity-auth-bridge",
            )
            self._reader.start()

        self.events.put({
            "kind": "auth_browser",
            "text": "TXITY's sign-in browser opened. Use its Discord login button.",
        })

    def _read_messages(self, process: subprocess.Popen[str]) -> None:
        stream = process.stdout
        if stream is None:
            return
        for line in stream:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue

            message_type = message.get("type")
            if message_type == "auth_token":
                token = message.get("token")
                user = clean_discord_user(message.get("user"))
                if is_auth_token(token):
                    self.events.put({"kind": "auth_token", "token": token, "user": user})
            elif message_type == "signed_out":
                self.events.put({"kind": "auth_signed_out"})
            elif message_type == "browser_error":
                message_text = message.get("text")
                if isinstance(message_text, str):
                    self.events.put({"kind": "auth_error", "text": message_text[:300]})
            elif message_type == "closed":
                self.events.put({
                    "kind": "auth_browser",
                    "text": "TXITY's sign-in browser was closed.",
                })

        with self._lock:
            if self.process is process:
                self.process = None

    def logout(self) -> None:
        """Ask the official page to run its own Discord logout handler."""
        with self._lock:
            process = self.process
            if process is None or process.poll() is not None or process.stdin is None:
                self.events.put({
                    "kind": "auth_error",
                    "text": "Open the TXITY sign-in browser before signing out.",
                })
                return
            try:
                process.stdin.write('{"command":"logout"}\n')
                process.stdin.flush()
            except OSError:
                self.events.put({
                    "kind": "auth_error",
                    "text": "Could not send the sign-out request to TXITY's browser.",
                })

    def close(self) -> None:
        with self._lock:
            process = self.process
            self.process = None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
