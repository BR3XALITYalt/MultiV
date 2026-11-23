"""Independent MP3 outputs for one or more Icecast servers."""

from __future__ import annotations

import base64
import queue
import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any
from urllib.parse import urlsplit


ICECAST_BITRATES = (64, 96, 128, 160, 192, 256, 320)
ICECAST_SAMPLE_RATE = 48_000
MP3_FRAME_SAMPLES = 1_152
INPUT_QUEUE_BLOCKS = 48
OUTPUT_QUEUE_PACKETS = 256


def normalize_icecast_destination(value: Any) -> dict[str, str]:
    """Validate one Icecast URL and its source credentials."""
    if not isinstance(value, dict):
        raise ValueError("Each Icecast destination must be a settings object.")

    raw_url = str(value.get("url") or "").strip()
    if not raw_url:
        raise ValueError("Enter an Icecast server URL including its mountpoint.")

    try:
        parsed = urlsplit(raw_url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("The Icecast server URL or port is invalid.") from exc

    if parsed.scheme.casefold() not in {"http", "https"}:
        raise ValueError("Icecast URLs must start with http:// or https://.")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("Enter the server URL without embedded username or password.")
    if parsed.query or parsed.fragment:
        raise ValueError("Icecast server URLs cannot contain a query or fragment.")
    if not parsed.path or parsed.path == "/" or any(char.isspace() for char in parsed.path):
        raise ValueError("Enter a mountpoint in the URL, such as /live.mp3.")
    if any(ord(char) < 32 or ord(char) == 127 for char in parsed.path):
        raise ValueError("The Icecast mountpoint contains invalid characters.")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("The Icecast server port must be between 1 and 65535.")

    username = str(value.get("username") or "source").strip()
    password = str(value.get("password") or "")
    if not username or ":" in username or "\r" in username or "\n" in username:
        raise ValueError("The Icecast source username is invalid.")
    if not password:
        raise ValueError("Enter the Icecast source password.")
    if "\r" in password or "\n" in password:
        raise ValueError("The Icecast source password contains invalid characters.")

    host = parsed.hostname
    default_port = 443 if parsed.scheme.casefold() == "https" else 8000
    selected_port = port or default_port
    display_host = f"[{host}]" if ":" in host else host
    display_name = f"{display_host}:{selected_port}{parsed.path}"

    return {
        "url": raw_url,
        "username": username,
        "password": password,
        "name": display_name,
        "scheme": parsed.scheme.casefold(),
        "host": host,
        "port": str(port or (443 if parsed.scheme.casefold() == "https" else 8000)),
        "mount": parsed.path,
    }


def _safe_header(value: str, limit: int = 160) -> str:
    """Remove header-breaking characters before placing text on the wire."""
    return " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())[:limit]


@dataclass
class _Destination:
    settings: dict[str, str]
    events: Any
    station_name: str
    station_description: str
    packets: queue.Queue[bytes | None] = field(
        default_factory=lambda: queue.Queue(maxsize=OUTPUT_QUEUE_PACKETS)
    )
    thread: threading.Thread | None = None
    socket_lock: threading.Lock = field(default_factory=threading.Lock)
    active_socket: socket.socket | ssl.SSLSocket | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    retry_attempt: int = 0

    @property
    def name(self) -> str:
        return self.settings["name"]

    def emit(self, state: str, detail: str = "") -> None:
        message = f"{self.name}: {state}"
        if detail:
            message += f" ({_safe_header(detail, 220)})"
        self.events.put({
            "kind": "tx_icecast_state",
            "name": self.name,
            "state": state,
            "text": message,
        })

    def close_socket(self) -> None:
        with self.socket_lock:
            active = self.active_socket
            self.active_socket = None
        if active is not None:
            try:
                active.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                active.close()
            except OSError:
                pass

    def connect(self) -> socket.socket | ssl.SSLSocket:
        settings = self.settings
        port = int(settings["port"])
        raw_socket = socket.create_connection(
            (settings["host"], port),
            timeout=8.0,
        )
        connection: socket.socket | ssl.SSLSocket = raw_socket
        if settings["scheme"] == "https":
            try:
                connection = ssl.create_default_context().wrap_socket(
                    raw_socket,
                    server_hostname=settings["host"],
                )
            except Exception:
                raw_socket.close()
                raise

        connection.settimeout(3.0)
        authorization = base64.b64encode(
            f'{settings["username"]}:{settings["password"]}'.encode("utf-8")
        ).decode("ascii")
        host_header = settings["host"]
        default_port = 443 if settings["scheme"] == "https" else 80
        if port != default_port:
            host_header = f"{host_header}:{port}"

        headers = [
            f'SOURCE {settings["mount"]} HTTP/1.0',
            f"Host: {host_header}",
            f"Authorization: Basic {authorization}",
            "User-Agent: MultiV",
            "Accept: */*",
            "Content-Type: audio/mpeg",
            "ice-public: 0",
            f"ice-name: {_safe_header(self.station_name)}",
            f"ice-description: {_safe_header(self.station_description)}",
            "Connection: keep-alive",
            "",
            "",
        ]
        connection.sendall("\r\n".join(headers).encode("utf-8"))

        response = bytearray()
        while b"\r\n\r\n" not in response and len(response) < 16_384:
            chunk = connection.recv(2_048)
            if not chunk:
                break
            response.extend(chunk)

        first_line = bytes(response).split(b"\r\n", 1)[0].decode(
            "latin-1",
            errors="replace",
        )
        match = re.search(r"(?:HTTP/\S+\s+|ICY\s+)(\d{3})(?:\s+(.*))?", first_line)
        if not match:
            connection.close()
            raise ConnectionError("The Icecast server returned no HTTP status.")
        status_code = int(match.group(1))
        reason = (match.group(2) or "No status text").strip()
        if status_code != 200:
            connection.close()
            reasons = {
                401: "source authentication failed",
                403: "source rejected the mountpoint or audio format",
                404: "mountpoint was not found",
                409: "mountpoint is already in use",
            }
            detail = reasons.get(status_code, _safe_header(reason, 120))
            raise ConnectionError(f"Icecast returned HTTP {status_code}: {detail}")

        connection.settimeout(3.0)
        return connection

    def enqueue_packet(self, payload: bytes) -> None:
        if self.stop_event.is_set():
            return
        try:
            self.packets.put_nowait(payload)
        except queue.Full:
            try:
                self.packets.get_nowait()
            except queue.Empty:
                pass
            try:
                self.packets.put_nowait(payload)
            except queue.Full:
                pass

    def run(self) -> None:
        delay = 1.0
        while not self.stop_event.is_set():
            connection = None
            try:
                self.emit("Connecting")
                connection = self.connect()
                with self.socket_lock:
                    self.active_socket = connection
                while True:
                    try:
                        self.packets.get_nowait()
                    except queue.Empty:
                        break
                self.retry_attempt = 0
                delay = 1.0
                self.emit("Connected")

                while not self.stop_event.is_set():
                    try:
                        payload = self.packets.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    if payload is None:
                        self.emit("Disconnected")
                        return
                    connection.sendall(payload)

            except (OSError, ssl.SSLError, ConnectionError, TimeoutError) as exc:
                if not self.stop_event.is_set():
                    self.retry_attempt += 1
                    detail = str(exc)[:220]
                    self.emit("Reconnecting", f"{detail}; retry in {delay:.0f}s")
                    self.stop_event.wait(delay)
                    delay = min(15.0, delay * 2.0)
            except Exception as exc:
                if not self.stop_event.is_set():
                    self.emit("Output error", str(exc)[:220])
                    self.stop_event.wait(5.0)
            finally:
                self.close_socket()

        self.emit("Disconnected")


class IcecastBroadcaster:
    """Encode source PCM once and fan the MP3 stream out to Icecast servers."""

    def __init__(
        self,
        destinations: list[dict[str, Any]],
        *,
        bitrate_kbps: int = 128,
        station_name: str = "MultiV",
        station_description: str = "",
        events: Any,
        sample_rate: int = ICECAST_SAMPLE_RATE,
        local_server: Any = None,
    ) -> None:
        try:
            bitrate = int(bitrate_kbps)
        except (TypeError, ValueError) as exc:
            raise ValueError("Icecast MP3 bitrate must be a whole number.") from exc
        if bitrate not in ICECAST_BITRATES:
            raise ValueError(
                "Icecast MP3 bitrate must be one of: "
                + ", ".join(str(value) for value in ICECAST_BITRATES)
                + " kbps."
            )
        if sample_rate != ICECAST_SAMPLE_RATE:
            raise ValueError("Icecast audio input must use 48 kHz PCM.")

        normalized = [normalize_icecast_destination(item) for item in destinations]
        if not normalized and local_server is None:
            raise ValueError("Add an enabled Icecast destination or enable the built-in server.")

        self.local_server = local_server
        self.bitrate = bitrate * 1_000
        self.sample_rate = sample_rate
        self.events = events
        self._input_queue: queue.Queue[Any] = queue.Queue(maxsize=INPUT_QUEUE_BLOCKS)
        self._closing = threading.Event()
        self._stopped = threading.Event()
        self._encoder_thread: threading.Thread | None = None
        self._destinations = [
            _Destination(
                item,
                events,
                _safe_header(station_name),
                _safe_header(station_description),
            )
            for item in normalized
        ]

    def start(self) -> None:
        if self.local_server is not None:
            self.local_server.start()
        for destination in self._destinations:
            destination.thread = threading.Thread(
                target=destination.run,
                daemon=True,
                name="multiv-icecast-" + str(len(destination.name)),
            )
            destination.thread.start()

        self._encoder_thread = threading.Thread(
            target=self._encode_audio,
            daemon=True,
            name="multiv-icecast-encoder",
        )
        self._encoder_thread.start()
        self.events.put({
            "kind": "tx_icecast_state",
            "name": "Icecast",
            "state": "Starting",
            "text": f"Starting {len(self._destinations)} Icecast output(s) at {self.bitrate // 1000} kbps MP3.",
        })

    def send_pcm(self, samples: Any, *, wait_for_space: bool = False) -> None:
        """Queue a copy of source PCM without making network calls in audio callbacks."""
        if self._closing.is_set() or self._stopped.is_set():
            return
        import numpy as np

        block = np.asarray(samples, dtype=np.float32).reshape(-1)
        if block.size == 0:
            return
        block = np.nan_to_num(block, nan=0.0, posinf=0.0, neginf=0.0).copy()

        if wait_for_space:
            while not self._closing.is_set():
                try:
                    self._input_queue.put(block, timeout=0.1)
                    return
                except queue.Full:
                    continue
            return

        try:
            self._input_queue.put_nowait(block)
        except queue.Full:
            try:
                self._input_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._input_queue.put_nowait(block)
            except queue.Full:
                pass

    def stop(self, wait: float = 2.0) -> None:
        """Flush the MP3 encoder, then disconnect each Icecast source."""
        if self._stopped.is_set():
            return
        self._closing.set()
        try:
            self._input_queue.put_nowait(None)
        except queue.Full:
            try:
                self._input_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._input_queue.put_nowait(None)
            except queue.Full:
                pass

        encoder = self._encoder_thread
        if encoder is not None and encoder is not threading.current_thread():
            encoder.join(timeout=wait)

        self._stopped.set()
        if self.local_server is not None:
            try:
                self.local_server.stop()
            except Exception as exc:
                self.events.put({
                    "kind": "tx_icecast_state",
                    "name": "Built-in server",
                    "state": "Stop error",
                    "text": f"Could not stop built-in Icecast server: {str(exc)[:200]}",
                })
        for destination in self._destinations:
            destination.stop_event.set()
            destination.close_socket()
            try:
                destination.packets.put_nowait(None)
            except queue.Full:
                try:
                    destination.packets.get_nowait()
                except queue.Empty:
                    pass
                try:
                    destination.packets.put_nowait(None)
                except queue.Full:
                    pass

        for destination in self._destinations:
            worker = destination.thread
            if worker is not None and worker is not threading.current_thread():
                worker.join(timeout=wait)
        self.events.put({
            "kind": "tx_icecast_state",
            "name": "Icecast",
            "state": "Stopped",
            "text": "Icecast outputs stopped.",
        })

    def _fanout(self, payload: bytes) -> None:
        if not payload:
            return
        for destination in self._destinations:
            destination.enqueue_packet(payload)
        if self.local_server is not None:
            self.local_server.publish(payload)

    def _encode_audio(self) -> None:
        import av
        import numpy as np

        codec = None
        pending = np.empty(0, dtype=np.float32)
        frame_size = MP3_FRAME_SAMPLES
        sample_position = 0
        next_deadline = time.perf_counter()

        try:
            codec = av.CodecContext.create("libmp3lame", "w")
            codec.sample_rate = self.sample_rate
            codec.layout = "mono"
            codec.format = "fltp"
            codec.bit_rate = self.bitrate
            codec.open()
            frame_size = int(codec.frame_size or MP3_FRAME_SAMPLES)

            while True:
                try:
                    block = self._input_queue.get(timeout=0.2)
                except queue.Empty:
                    if self._closing.is_set():
                        break
                    continue
                if block is None:
                    break

                pending = np.concatenate((pending, block))
                while pending.size >= frame_size:
                    remaining = next_deadline - time.perf_counter()
                    if remaining > 0 and self._closing.wait(remaining):
                        break
                    now = time.perf_counter()
                    if now - next_deadline > 0.5:
                        next_deadline = now

                    samples = pending[:frame_size]
                    pending = pending[frame_size:]
                    frame = av.AudioFrame.from_ndarray(
                        samples.reshape(1, -1),
                        format="fltp",
                        layout="mono",
                    )
                    frame.sample_rate = self.sample_rate
                    frame.pts = sample_position
                    frame.time_base = Fraction(1, self.sample_rate)
                    sample_position += frame_size
                    for packet in codec.encode(frame):
                        self._fanout(bytes(packet))
                    next_deadline += frame_size / self.sample_rate

            if pending.size:
                padded = np.zeros(frame_size, dtype=np.float32)
                padded[:min(frame_size, pending.size)] = pending[:frame_size]
                frame = av.AudioFrame.from_ndarray(
                    padded.reshape(1, -1),
                    format="fltp",
                    layout="mono",
                )
                frame.sample_rate = self.sample_rate
                frame.pts = sample_position
                frame.time_base = Fraction(1, self.sample_rate)
                for packet in codec.encode(frame):
                    self._fanout(bytes(packet))

            for packet in codec.encode(None):
                self._fanout(bytes(packet))

        except Exception as exc:
            self.events.put({
                "kind": "tx_icecast_state",
                "name": "Icecast",
                "state": "Encoder error",
                "text": f"Icecast MP3 encoder error: {str(exc)[:240]}",
            })
        finally:
            for destination in self._destinations:
                try:
                    destination.packets.put_nowait(None)
                except queue.Full:
                    try:
                        destination.packets.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        destination.packets.put_nowait(None)
                    except queue.Full:
                        pass
