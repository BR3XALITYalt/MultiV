"""TXITY network client for station listings and live service status."""
from __future__ import annotations

import json
import queue
import secrets
import threading
import uuid
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

from discord_auth import (
    clean_discord_user,
    is_auth_token,
    safe_error_text,
    write_diagnostic_text,
)
from station_directory import (
    BASE_URL,
    describe_status_socket_error,
    fetch_text,
    parse_users_listing,
)
from station_upload import (
    image_file_to_data_url,
    upload_station_image as post_station_image,
)

CONTROL_URL = "wss://txity.radiodiscussion.org/ws/control"


class TxityClient:
    """Perform network I/O off the GUI thread and publish results via a queue."""

    def __init__(self) -> None:
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.stop_event = threading.Event()
        self.refresh_lock = threading.Lock()
        self.auth_lock = threading.Lock()
        self.reconnect_event = threading.Event()
        self.session_id = str(uuid.uuid4())
        self.session_secret = secrets.token_hex(32)
        self.auth_token = ""
        self.discord_user: dict[str, str] | None = None
        self.ws: Any = None
        self.ws_thread: threading.Thread | None = None
        self.control_send_lock = threading.Lock()
        self.control_generation = 0
        self.image_upload_lock = threading.Lock()

    def set_discord_auth(self, token: Any, user: Any = None) -> None:
        """Attach or disconnect a token received through TXITY's OAuth callback."""
        clean_token = token if is_auth_token(token) else ""
        clean_user = clean_discord_user(user) if clean_token else None
        with self.auth_lock:
            if clean_token == self.auth_token:
                self.discord_user = clean_user
                return
            had_auth = bool(self.auth_token)
            active_socket = self.ws
            self.auth_token = clean_token
            self.discord_user = clean_user

        if active_socket is not None and had_auth and not clean_token:
            try:
                if active_socket.sock and active_socket.sock.connected:
                    active_socket.send(json.dumps({"type": "discordLogout", "discordLogout": True}))
            except Exception:
                pass

        self.reconnect_event.set()
        if active_socket is not None:
            try:
                active_socket.close()
            except Exception:
                pass

    def transmission_credentials(self) -> tuple[str, str, str]:
        """Return the in-memory credentials needed for TXITY's audio socket."""
        with self.auth_lock:
            if not self.auth_token:
                raise RuntimeError("Sign in with Discord before starting transmission.")
            return self.session_id, self.session_secret, self.auth_token

    def control_connected(self) -> bool:
        with self.auth_lock:
            active_socket = self.ws
            if not self.auth_token or active_socket is None:
                return False
            socket = getattr(active_socket, "sock", None)
            return bool(socket is not None and getattr(socket, "connected", False))

    def send_control_message(self, message: dict[str, Any]) -> None:
        """Send a TXITY config or PTT message over the authenticated control socket."""
        with self.auth_lock:
            active_socket = self.ws
            authenticated = bool(self.auth_token)
        if not authenticated:
            raise RuntimeError("Sign in with Discord before starting transmission.")
        if active_socket is None:
            raise ConnectionError("TXITY control connection is not open.")

        with self.control_send_lock:
            socket = getattr(active_socket, "sock", None)
            if socket is None or not getattr(socket, "connected", False):
                raise ConnectionError("TXITY control connection is not ready.")
            active_socket.send(json.dumps(message, separators=(",", ":")))

    def connect(self) -> None:
        if self.ws_thread and self.ws_thread.is_alive():
            if self.stop_event.is_set():
                self.stop_event.clear()
                self.reconnect_event.set()
                if self.ws is not None:
                    try:
                        self.ws.close()
                    except Exception:
                        pass
            return
        self.stop_event.clear()
        self.reconnect_event.clear()
        self.events.put({"kind": "connection", "text": "Connecting to TXITY..."})
        threading.Thread(target=self._fetch_public_data, daemon=True, name="txity-http").start()
        self.ws_thread = threading.Thread(
            target=self._run_status_socket, daemon=True, name="txity-status"
        )
        self.ws_thread.start()

    def load_public_data(self) -> None:
        """Fetch the public configuration and station listing without a socket."""
        threading.Thread(
            target=self._fetch_public_data,
            daemon=True,
            name="txity-public-data",
        ).start()

    def disconnect(self, wait: float = 0.0) -> None:
        self.stop_event.set()
        self.reconnect_event.set()

        active_socket = self.ws
        if active_socket is not None:
            try:
                active_socket.close()
            except Exception:
                pass

        thread = self.ws_thread
        timeout = max(0.0, float(wait))
        if timeout and thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)

        self.events.put({"kind": "connection", "text": "Disconnected"})

    @property
    def station_image_uploading(self) -> bool:
        return self.image_upload_lock.locked()

    def upload_station_image_file(self, path: str, *, kind: str = "logo") -> None:
        """Upload a station image with the active in-memory TXITY session."""
        with self.auth_lock:
            session_id = self.session_id
            auth_token = self.auth_token
        if not auth_token:
            raise RuntimeError("Sign in with Discord before uploading a station image.")
        if not self.image_upload_lock.acquire(blocking=False):
            raise RuntimeError("A station image upload is already in progress.")

        def worker() -> None:
            try:
                data_url = image_file_to_data_url(path)
                with self.auth_lock:
                    session_is_current = (
                        self.session_id == session_id
                        and self.auth_token == auth_token
                    )
                if not session_is_current:
                    raise RuntimeError(
                        "The Discord session changed during the upload. Sign in again."
                    )
                image_url = post_station_image(
                    session_id,
                    auth_token,
                    data_url,
                    kind=kind,
                )
                self.events.put({
                    "kind": "station_image_upload_success",
                    "image_kind": kind,
                    "url": image_url,
                })
            except Exception as exc:
                self.events.put({
                    "kind": "station_image_upload_error",
                    "image_kind": kind,
                    "text": safe_error_text(str(exc), 500),
                })
            finally:
                self.image_upload_lock.release()

        thread = threading.Thread(
            target=worker,
            daemon=True,
            name="txity-image-upload",
        )
        try:
            thread.start()
        except Exception:
            self.image_upload_lock.release()
            raise

    def refresh_stations(self) -> None:
        if not self.refresh_lock.acquire(blocking=False):
            return

        def worker() -> None:
            try:
                payload = json.loads(fetch_text(f"{BASE_URL}/users.json"))
                metrics, stations = parse_users_listing(payload)
                self.events.put({
                    "kind": "users_listing",
                    "metrics": metrics,
                    "rows": [station.as_record() for station in stations],
                })
            except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError) as exc:
                self.events.put({
                    "kind": "stations_error",
                    "text": f"Station refresh failed: {safe_error_text(str(exc))}",
                })
            finally:
                self.refresh_lock.release()

        threading.Thread(target=worker, daemon=True, name="txity-stations").start()

    def _fetch_public_data(self) -> None:
        try:
            config = json.loads(fetch_text(f"{BASE_URL}/api/config"))
            if isinstance(config, dict):
                self.events.put({"kind": "config", "data": config})
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            self.events.put({"kind": "error", "text": f"Public configuration unavailable: {exc}"})
        self.refresh_stations()

    def _run_status_socket(self) -> None:
        import websocket

        # Reuse this app session across reconnects. TXITY itself supplies the
        # signed token after the official Discord flow completes.

        status_received = threading.Event()

        def on_open(ws: websocket.WebSocketApp) -> None:
            with self.auth_lock:
                self.control_generation += 1
            self.events.put({
                "kind": "connection",
                "text": "Connected (Discord session supplied)" if auth_token else "Connected (public status only)",
            })
            # The status request is read-only. No config, PTT, or audio messages
            # are sent by this desktop client.
            with self.control_send_lock:
                ws.send(json.dumps({"type": "status"}))

            def heartbeat() -> None:
                while not self.stop_event.wait(1.0):
                    try:
                        with self.control_send_lock:
                            ws.send(json.dumps({"type": "status"}))
                    except Exception:
                        return

            threading.Thread(target=heartbeat, daemon=True, name="txity-status-heartbeat").start()

        def on_message(_ws: websocket.WebSocketApp, message: str | bytes) -> None:
            if not isinstance(message, str):
                return
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                return
            if not isinstance(data, dict):
                return
            message_type = data.get("type")
            if message_type == "status":
                if not status_received.is_set():
                    status_received.set()
                    self.events.put({
                        "kind": "connection",
                        "text": "Connected",
                    })
                self.events.put({"kind": "status", "data": data})
            elif message_type in {"notice", "kicked", "banned"}:
                self.events.put({"kind": "tx_server_message", "data": data})

        def on_error(_ws: websocket.WebSocketApp, error: Any) -> None:
            if self.stop_event.is_set():
                return
            details = describe_status_socket_error(error)
            write_diagnostic_text("status-websocket-error", details)
            self.events.put({"kind": "socket_error", "text": details})

        def on_close(_ws: websocket.WebSocketApp, code: Any, reason: Any) -> None:
            if self.stop_event.is_set():
                return
            close_text = f"Status WebSocket closed (code: {code!r}"
            if reason:
                close_text += f", reason: {safe_error_text(str(reason), max_chars=300)}"
            close_text += ")."
            self.events.put({"kind": "connection", "text": close_text})

        try:
            while not self.stop_event.is_set():
                ws: websocket.WebSocketApp | None = None
                try:
                    with self.auth_lock:
                        auth_token = self.auth_token
                    query_values = {
                        "session": self.session_id,
                        "sessionSecret": self.session_secret,
                    }
                    if auth_token:
                        query_values["authToken"] = auth_token
                    socket_url = f"{CONTROL_URL}?{urlencode(query_values)}"
                    ws = websocket.WebSocketApp(
                        socket_url,
                        on_open=on_open,
                        on_message=on_message,
                        on_error=on_error,
                        on_close=on_close,
                    )
                    with self.auth_lock:
                        current_token = self.auth_token
                        self.ws = ws
                    if current_token != auth_token:
                        self.reconnect_event.set()
                        ws.close()
                    ws.run_forever(
                        origin=BASE_URL,
                        ping_interval=25,
                        ping_timeout=10,
                        reconnect=0,
                    )
                except Exception as exc:
                    if not self.stop_event.is_set():
                        details = describe_status_socket_error(exc)
                        write_diagnostic_text("status-websocket-client-error", details)
                        self.events.put({"kind": "socket_error", "text": details})
                finally:
                    if ws is not None:
                        with self.auth_lock:
                            if self.ws is ws:
                                self.ws = None

                if not self.stop_event.is_set():
                    self.events.put({
                        "kind": "connection",
                        "text": "TXITY control connection lost; reconnecting...",
                    })
                    if self.reconnect_event.wait(4):
                        self.reconnect_event.clear()
        finally:
            with self.auth_lock:
                self.ws = None
