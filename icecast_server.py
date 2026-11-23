"""Embedded Icecast-compatible MP3 listener server.

The server exposes an HTTP audio mount for standard streaming players. Audio is
published directly from MultiV's shared MP3 encoder, without a second encode.
"""
from __future__ import annotations

import base64
import hmac
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit


class _Listener:
    def __init__(self, max_queue: int = 128) -> None:
        self.packets: queue.Queue[bytes | None] = queue.Queue(maxsize=max_queue)
        self.closed = threading.Event()

    def publish(self, packet: bytes) -> None:
        if self.closed.is_set():
            return
        try:
            self.packets.put_nowait(packet)
        except queue.Full:
            try:
                self.packets.get_nowait()
            except queue.Empty:
                pass
            try:
                self.packets.put_nowait(packet)
            except queue.Full:
                pass

    def close(self) -> None:
        self.closed.set()
        try:
            self.packets.put_nowait(None)
        except queue.Full:
            try:
                self.packets.get_nowait()
            except queue.Empty:
                pass
            try:
                self.packets.put_nowait(None)
            except queue.Full:
                pass


class EmbeddedIcecastServer:
    """Small embedded HTTP streaming server for one MP3 mountpoint."""

    def __init__(
        self,
        *,
        host: str = "0.0.0.0",
        port: int = 8000,
        mount: str = "/live.mp3",
        name: str = "MultiV",
        description: str = "",
        listener_password: str = "",
        max_listeners: int = 25,
        events: Any = None,
    ) -> None:
        self.host = str(host or "0.0.0.0").strip()
        self.port = int(port)
        self.mount = "/" + str(mount or "live.mp3").strip().lstrip("/")
        self.name = self._header(name) or "MultiV"
        self.description = self._header(description)
        self.listener_password = str(listener_password or "")
        self.max_listeners = int(max_listeners)
        self.events = events
        if not 1 <= self.port <= 65535:
            raise ValueError("Embedded Icecast port must be between 1 and 65535.")
        if len(self.mount) > 200 or any(c.isspace() or ord(c) < 32 for c in self.mount):
            raise ValueError("Embedded Icecast mountpoint is invalid.")
        if not 1 <= self.max_listeners <= 500:
            raise ValueError("Maximum listeners must be between 1 and 500.")
        if any(c in self.listener_password for c in "\r\n"):
            raise ValueError("Listener password contains invalid characters.")
        self._lock = threading.RLock()
        self._listeners: set[_Listener] = set()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._started = threading.Event()
        self._stopping = threading.Event()

    @staticmethod
    def _header(value: str) -> str:
        return " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())[:160]

    @property
    def listener_count(self) -> int:
        with self._lock:
            return len(self._listeners)

    @property
    def is_running(self) -> bool:
        return self._server is not None and self._started.is_set() and not self._stopping.is_set()

    def _emit(self, state: str, detail: str = "") -> None:
        if self.events is not None:
            self.events.put({
                "kind": "tx_icecast_state",
                "name": "Built-in server",
                "state": state,
                "text": f"Built-in Icecast server: {state}" + (f" ({detail})" if detail else ""),
            })

    def start(self) -> None:
        if self.is_running:
            return
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, _format: str, *_args) -> None:
                return

            def do_GET(self) -> None:
                path = urlsplit(self.path).path
                if path != owner.mount:
                    self.send_error(404, "Mountpoint not found")
                    return
                if owner.listener_password and not owner._authorized(self.headers.get("Authorization", "")):
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="MultiV stream"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                listener = _Listener()
                with owner._lock:
                    if owner._stopping.is_set():
                        self.send_error(503, "Server stopping")
                        return
                    if len(owner._listeners) >= owner.max_listeners:
                        self.send_error(503, "Listener limit reached")
                        return
                    owner._listeners.add(listener)
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "audio/mpeg")
                    self.send_header("Cache-Control", "no-cache, no-store")
                    self.send_header("Connection", "close")
                    self.send_header("icy-name", owner.name)
                    self.send_header("icy-description", owner.description)
                    self.send_header("icy-br", "128")
                    self.send_header("icy-pub", "0")
                    self.end_headers()
                    while not owner._stopping.is_set() and not listener.closed.is_set():
                        try:
                            packet = listener.packets.get(timeout=1.0)
                        except queue.Empty:
                            continue
                        if packet is None:
                            break
                        self.wfile.write(packet)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    listener.close()
                    with owner._lock:
                        owner._listeners.discard(listener)

        try:
            self._server = ThreadingHTTPServer((self.host, self.port), Handler)
            self._server.daemon_threads = True
            self._server.timeout = 0.5
            self._stopping.clear()
            self._thread = threading.Thread(
                target=self._server.serve_forever,
                kwargs={"poll_interval": 0.25},
                name="multiv-embedded-icecast",
                daemon=True,
            )
            self._thread.start()
            self._started.set()
            self._emit("Listening", f"http://{self._display_host()}:{self.port}{self.mount}")
        except OSError:
            self._server = None
            raise

    def _display_host(self) -> str:
        return "127.0.0.1" if self.host in {"0.0.0.0", "::"} else self.host

    def _authorized(self, header: str) -> bool:
        try:
            scheme, token = header.split(" ", 1)
            if scheme.casefold() != "basic":
                return False
            decoded = base64.b64decode(token, validate=True).decode("utf-8")
            _, password = decoded.split(":", 1)
            return hmac.compare_digest(password, self.listener_password)
        except (ValueError, UnicodeError):
            return False

    def publish(self, packet: bytes) -> None:
        if not packet or not self.is_running:
            return
        with self._lock:
            listeners = tuple(self._listeners)
        for listener in listeners:
            listener.publish(packet)

    def stop(self) -> None:
        server = self._server
        if server is None:
            return
        self._stopping.set()
        with self._lock:
            listeners = tuple(self._listeners)
            self._listeners.clear()
        for listener in listeners:
            listener.close()
        server.shutdown()
        server.server_close()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3.0)
        self._server = None
        self._thread = None
        self._started.clear()
        self._emit("Stopped")
