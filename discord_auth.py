"""Manage the embedded TXITY login browser without persisting its auth token."""

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
    """Reject malformed values before they are passed to the TXITY socket."""
    return (
        isinstance(value, str)
        and 16 <= len(value) <= 8192
        and not any(character.isspace() or ord(character) < 32 for character in value)
    )


def _storage_directory() -> Path:
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return root / "TXITPY" / "browser-profile"


class DiscordAuthBrowser:
    """Launch TXITY's official page in a persistent embedded browser process."""

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
                self.events.put({
                    "kind": "auth_error",
                    "text": "The TXITY browser helper is missing.",
                })
                return

            command = [
                sys.executable,
                str(helper),
                "--storage-path",
                str(_storage_directory()),
            ]
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

            try:
                self.process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    creationflags=creationflags,
                )
            except OSError as exc:
                self.events.put({
                    "kind": "auth_error",
                    "text": f"Could not open the TXITY sign-in browser: {exc}",
                })
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
                if is_auth_token(token):
                    self.events.put({"kind": "auth_token", "token": token})
            elif message_type == "signed_out":
                self.events.put({"kind": "auth_signed_out"})
            elif message_type == "browser_error":
                text = message.get("text")
                if isinstance(text, str):
                    self.events.put({"kind": "auth_error", "text": text[:300]})
            elif message_type == "closed":
                self.events.put({
                    "kind": "auth_browser",
                    "text": "TXITY's sign-in browser was closed.",
                })

        with self._lock:
            if self.process is process:
                self.process = None

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
