"""Per-destination audio encoders and Icecast source connections."""

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


ICECAST_BITRATES = (32, 48, 64, 96, 128, 160, 192, 256, 320)
ICECAST_SAMPLE_RATE = 48_000
INPUT_QUEUE_BLOCKS = 48
OUTPUT_QUEUE_PACKETS = 256

ICECAST_FORMATS = {
    "mp3": {
        "label": "MP3",
        "codec": "libmp3lame",
        "content_type": "audio/mpeg",
        "container": None,
        "frame_size": 1152,
    },
    "aac_plus": {
        "label": "AAC+ (HE-AAC)",
        "codec": "libfdk_aac",
        "content_type": "audio/aacp",
        "container": "adts",
        "frame_size": 1024,
    },
    "ogg_vorbis": {
        "label": "Ogg Vorbis",
        "codec": "libvorbis",
        "content_type": "application/ogg",
        "container": "ogg",
        "frame_size": 1024,
    },
    "ogg_opus": {
        "label": "Ogg Opus",
        "codec": "libopus",
        "content_type": "application/ogg",
        "container": "ogg",
        "frame_size": 960,
    },
    "ogg_flac": {
        "label": "Ogg FLAC",
        "codec": "flac",
        "content_type": "application/ogg",
        "container": "ogg",
        "frame_size": 4096,
        "lossless": True,
    },
}


def available_icecast_formats() -> dict[str, str]:
    """Return codecs available in the installed PyAV/FFmpeg build."""
    import av

    available: dict[str, str] = {}
    for key, spec in ICECAST_FORMATS.items():
        try:
            av.Codec(spec["codec"], "w")
        except (av.error.FFmpegError, ValueError):
            continue
        available[key] = str(spec["label"])
    return available


def normalize_icecast_destination(value: Any) -> dict[str, str]:
    """Validate one Icecast URL, source credentials, codec, and bitrate."""
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

    audio_format = str(value.get("format") or "mp3").strip().casefold()
    if audio_format not in ICECAST_FORMATS:
        raise ValueError("Choose a supported Icecast audio format.")
    if audio_format == "ogg_flac":
        bitrate = 0
    else:
        try:
            bitrate = int(value.get("bitrate", 128))
        except (TypeError, ValueError) as exc:
            raise ValueError("The output bitrate must be a whole number.") from exc
        if bitrate not in ICECAST_BITRATES:
            raise ValueError(
                "Output bitrate must be one of: "
                + ", ".join(str(item) for item in ICECAST_BITRATES)
                + " kbps."
            )
        if audio_format == "aac_plus" and bitrate > 128:
            raise ValueError("AAC+ bitrate must be 128 kbps or lower.")
        if audio_format == "ogg_opus" and bitrate < 48:
            raise ValueError("Ogg Opus bitrate must be at least 48 kbps.")

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
        "port": str(port or default_port),
        "mount": parsed.path,
        "format": audio_format,
        "bitrate": str(bitrate),
    }


def _safe_header(value: str, limit: int = 160) -> str:
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

    @property
    def name(self) -> str:
        return self.settings["name"]

    @property
    def audio_format(self) -> str:
        return self.settings["format"]

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
        raw_socket = socket.create_connection((settings["host"], port), timeout=8.0)
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
        spec = ICECAST_FORMATS[settings["format"]]
        headers = [
            f'SOURCE {settings["mount"]} HTTP/1.0',
            f"Host: {host_header}",
            f"Authorization: Basic {authorization}",
            "User-Agent: MultiV",
            "Accept: */*",
            f'Content-Type: {spec["content_type"]}',
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
        first_line = bytes(response).split(b"\r\n", 1)[0].decode("latin-1", errors="replace")
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
            raise ConnectionError(
                f"Icecast returned HTTP {status_code}: "
                f"{reasons.get(status_code, _safe_header(reason, 120))}"
            )
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
                self.emit("Connecting", ICECAST_FORMATS[self.audio_format]["label"])
                connection = self.connect()
                with self.socket_lock:
                    self.active_socket = connection
                while True:
                    try:
                        self.packets.get_nowait()
                    except queue.Empty:
                        break
                delay = 1.0
                self.emit("Connected", ICECAST_FORMATS[self.audio_format]["label"])
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
                    self.emit("Reconnecting", f"{str(exc)[:180]}; retry in {delay:.0f}s")
                    self.stop_event.wait(delay)
                    delay = min(15.0, delay * 2.0)
            except Exception as exc:
                if not self.stop_event.is_set():
                    self.emit("Output error", str(exc)[:220])
                    self.stop_event.wait(5.0)
            finally:
                self.close_socket()
        self.emit("Disconnected")


class _FanoutWriter:
    """File-like output consumed by PyAV's Ogg/ADTS muxers."""

    def __init__(self, destinations: list[_Destination], local_server: Any = None) -> None:
        self.destinations = destinations
        self.local_server = local_server

    def write(self, payload: bytes) -> int:
        data = bytes(payload)
        for destination in self.destinations:
            destination.enqueue_packet(data)
        if self.local_server is not None:
            self.local_server.publish(data)
        return len(data)

    def flush(self) -> None:
        return None

    def writable(self) -> bool:
        return True


@dataclass
class _EncoderGroup:
    audio_format: str
    bitrate_kbps: int
    destinations: list[_Destination]
    local_server: Any = None
    input_queue: queue.Queue[Any] = field(
        default_factory=lambda: queue.Queue(maxsize=INPUT_QUEUE_BLOCKS)
    )
    thread: threading.Thread | None = None


class IcecastBroadcaster:
    """Encode each distinct format/bitrate once, then fan out to matching outputs."""

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
        if sample_rate != ICECAST_SAMPLE_RATE:
            raise ValueError("Icecast audio input must use 48 kHz PCM.")
        try:
            default_bitrate = int(bitrate_kbps)
        except (TypeError, ValueError) as exc:
            raise ValueError("Built-in Icecast MP3 bitrate must be a whole number.") from exc
        if default_bitrate not in ICECAST_BITRATES:
            raise ValueError("Built-in Icecast MP3 bitrate is invalid.")
        normalized = [normalize_icecast_destination(item) for item in destinations]
        if not normalized and local_server is None:
            raise ValueError("Add at least one enabled Icecast destination or enable the built-in server.")
        self.local_server = local_server
        self.bitrate = default_bitrate * 1_000
        self.sample_rate = sample_rate
        self.events = events
        self._closing = threading.Event()
        self._stopped = threading.Event()
        self._destinations = [
            _Destination(item, events, _safe_header(station_name), _safe_header(station_description))
            for item in normalized
        ]

        grouped: dict[tuple[str, int], list[_Destination]] = {}
        for destination in self._destinations:
            key = (
                destination.audio_format,
                int(destination.settings["bitrate"]),
            )
            grouped.setdefault(key, []).append(destination)
        if local_server is not None:
            local_format = str(local_server.audio_format)
            local_bitrate = int(local_server.bitrate_kbps)
            grouped.setdefault((local_format, local_bitrate), [])

        self._groups = [
            _EncoderGroup(
                audio_format,
                bitrate,
                items,
                local_server
                if local_server is not None
                and audio_format == local_server.audio_format
                and bitrate == int(local_server.bitrate_kbps)
                else None,
            )
            for (audio_format, bitrate), items in grouped.items()
        ]

    def start(self) -> None:
        local_server = self.local_server
        if local_server is not None:
            try:
                local_server.start()
            except Exception as exc:
                self.events.put({
                    "kind": "tx_icecast_state",
                    "name": "Built-in server",
                    "state": "Start error",
                    "text": f"Could not start the built-in Icecast server: {str(exc)[:220]}",
                })
                self.local_server = None
                for group in self._groups:
                    if group.local_server is local_server:
                        group.local_server = None
                self._groups = [
                    group for group in self._groups
                    if group.destinations or group.local_server is not None
                ]
                if not self._destinations:
                    raise RuntimeError(
                        "The built-in Icecast server could not start and no remote Icecast destinations are available."
                    ) from exc

        for destination in self._destinations:
            destination.thread = threading.Thread(
                target=destination.run,
                daemon=True,
                name="multiv-icecast-output",
            )
            destination.thread.start()
        for group in self._groups:
            group.thread = threading.Thread(
                target=self._encode_group,
                args=(group,),
                daemon=True,
                name=f"multiv-icecast-{group.audio_format}",
            )
            group.thread.start()
        self.events.put({
            "kind": "tx_icecast_state",
            "name": "Icecast",
            "state": "Starting",
            "text": f"Starting {len(self._destinations)} Icecast output(s) with independent formats.",
        })

    def _fanout(self, payload: bytes) -> None:
        """Send an already encoded payload to every configured listener."""
        if not payload:
            return
        for destination in self._destinations:
            destination.enqueue_packet(payload)
        if self.local_server is not None:
            self.local_server.publish(payload)

    def send_pcm(self, samples: Any, *, wait_for_space: bool = False) -> None:
        if self._closing.is_set() or self._stopped.is_set():
            return
        import numpy as np

        block = np.asarray(samples, dtype=np.float32).reshape(-1)
        if block.size == 0:
            return
        block = np.nan_to_num(block, nan=0.0, posinf=0.0, neginf=0.0).copy()
        for group in self._groups:
            if wait_for_space:
                while not self._closing.is_set():
                    worker = group.thread
                    if worker is None or not worker.is_alive():
                        break
                    try:
                        group.input_queue.put(block, timeout=0.1)
                        break
                    except queue.Full:
                        continue
            else:
                try:
                    group.input_queue.put_nowait(block)
                except queue.Full:
                    try:
                        group.input_queue.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        group.input_queue.put_nowait(block)
                    except queue.Full:
                        pass

    def stop(self, wait: float = 1.0) -> None:
        if self._stopped.is_set():
            return
        self._closing.set()
        for group in self._groups:
            try:
                group.input_queue.put_nowait(None)
            except queue.Full:
                try:
                    group.input_queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    group.input_queue.put_nowait(None)
                except queue.Full:
                    pass
        for group in self._groups:
            if group.thread is not None and group.thread is not threading.current_thread():
                group.thread.join(timeout=wait)
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
            if destination.thread is not None and destination.thread is not threading.current_thread():
                destination.thread.join(timeout=wait)
        self.events.put({
            "kind": "tx_icecast_state",
            "name": "Icecast",
            "state": "Stopped",
            "text": "Icecast outputs stopped.",
        })

    def _encode_group(self, group: _EncoderGroup) -> None:
        import av
        import numpy as np

        spec = ICECAST_FORMATS[group.audio_format]
        writer = _FanoutWriter(group.destinations, group.local_server)
        codec = None
        container = None
        stream = None
        pending = np.empty(0, dtype=np.float32)
        sample_position = 0
        next_deadline = time.perf_counter()
        frame_size = int(spec["frame_size"])

        try:
            if spec["container"] is None:
                codec = av.CodecContext.create(spec["codec"], "w")
                codec.sample_rate = self.sample_rate
                codec.layout = "mono"
                codec.format = "fltp"
                codec.bit_rate = group.bitrate_kbps * 1000
                codec.open()
                frame_size = int(codec.frame_size or frame_size)
            else:
                container = av.open(writer, mode="w", format=spec["container"])
                stream = container.add_stream(spec["codec"], rate=self.sample_rate)
                stream.layout = "mono"
                if not spec.get("lossless"):
                    stream.bit_rate = group.bitrate_kbps * 1000
                if group.audio_format == "aac_plus":
                    stream.codec_context.options = {"profile": "aac_he"}
                frame_size = int(stream.codec_context.frame_size or frame_size)

            while True:
                try:
                    block = group.input_queue.get(timeout=0.2)
                except queue.Empty:
                    if self._closing.is_set():
                        break
                    continue
                if block is None:
                    break
                pending = np.concatenate((pending, block))
                while pending.size >= frame_size:
                    remaining = next_deadline - time.perf_counter()
                    if remaining > 0 and not self._closing.is_set():
                        self._closing.wait(remaining)
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
                    if codec is not None:
                        for packet in codec.encode(frame):
                            writer.write(bytes(packet))
                    else:
                        for packet in stream.encode(frame):
                            container.mux(packet)
                    next_deadline += frame_size / self.sample_rate

            if pending.size:
                samples = np.zeros(frame_size, dtype=np.float32)
                samples[:min(frame_size, pending.size)] = pending[:frame_size]
                frame = av.AudioFrame.from_ndarray(
                    samples.reshape(1, -1),
                    format="fltp",
                    layout="mono",
                )
                frame.sample_rate = self.sample_rate
                frame.pts = sample_position
                frame.time_base = Fraction(1, self.sample_rate)
                if codec is not None:
                    for packet in codec.encode(frame):
                        writer.write(bytes(packet))
                else:
                    for packet in stream.encode(frame):
                        container.mux(packet)

            if codec is not None:
                for packet in codec.encode(None):
                    writer.write(bytes(packet))
            elif stream is not None:
                for packet in stream.encode(None):
                    container.mux(packet)
                container.close()
                container = None
        except Exception as exc:
            for destination in group.destinations:
                destination.emit("Encoder error", str(exc)[:180])
            self.events.put({
                "kind": "tx_icecast_state",
                "name": "Built-in server" if group.local_server else "Icecast",
                "state": "Encoder error",
                "text": f"{spec['label']} encoder error: {str(exc)[:220]}",
            })
        finally:
            if container is not None:
                try:
                    container.close()
                except Exception:
                    pass
            if codec is not None:
                try:
                    codec.close()
                except Exception:
                    pass
