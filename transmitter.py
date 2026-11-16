"""TXITY virtual transmitter: authenticated control and mono PCM audio streaming."""

from __future__ import annotations

import math
import queue
import re
import threading
import time
from typing import Any
from urllib.parse import urlencode


BASE_URL = "https://txity.radiodiscussion.org"
AUDIO_URL = "wss://txity.radiodiscussion.org/ws/audio"
SAMPLE_RATE_HZ = 48_000
BATCH_SAMPLES = 512
FRAME_BYTES = BATCH_SAMPLES * 4
AUDIO_QUEUE_FRAMES = 32
SUPPORTED_MODES = ("AM", "USB", "LSB", "USB+C", "LSB+C", "DSB", "CW")


def parse_rf_frequency(value: Any) -> int:
    """Parse TXITY-style frequency text into integer hertz."""
    text = str(value or "").strip().replace(",", "")
    match = re.fullmatch(
        r"([0-9]+(?:\.[0-9]+)?)\s*(hz|hertz|khz|kilohertz|mhz|megahertz)?",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        raise ValueError("Enter a frequency such as 7.250 MHz, 7250 kHz, or 7250000 Hz.")

    number = float(match.group(1))
    if not math.isfinite(number) or number <= 0:
        raise ValueError("Frequency must be a positive number.")

    unit = (match.group(2) or "").casefold()
    if unit in {"hz", "hertz"}:
        multiplier = 1
    elif unit in {"khz", "kilohertz"}:
        multiplier = 1_000
    elif unit in {"mhz", "megahertz"}:
        multiplier = 1_000_000
    elif number >= 1_000_000:
        multiplier = 1
    elif number >= 1_000:
        multiplier = 1_000
    else:
        multiplier = 1_000_000

    frequency = int(round(number * multiplier))
    if frequency <= 0:
        raise ValueError("Frequency must be greater than zero.")
    return frequency


def build_audio_socket_url(session_id: str, session_secret: str, auth_token: str) -> str:
    """Build the TXITY audio socket URL without logging its credentials."""
    if not session_id or not session_secret or not auth_token:
        raise ValueError("Discord login is required before opening the audio connection.")
    query = urlencode({
        "session": session_id,
        "sessionSecret": session_secret,
        "authToken": auth_token,
    })
    return f"{AUDIO_URL}?{query}"


def build_transmit_config(
    *,
    station_name: str,
    description: str,
    frequency: Any,
    mode: str,
    station_dbm: Any,
    allow_overlap: bool,
    auth_token: str,
    cw_text: str = "CQ CQ DE TXITY",
    cw_wpm: Any = 18,
    station_logo_url: str = "",
    station_details: str = "",
    station_page_layout: str = "",
) -> dict[str, Any]:
    """Create the TXITY config message using the website's currentConfig schema."""
    name = str(station_name or "").strip()
    if not name:
        raise ValueError("Station name is required before starting transmission.")
    if len(name) > 48:
        raise ValueError("Station name must be 48 characters or fewer.")
    if not auth_token:
        raise ValueError("Sign in with Discord before starting transmission.")

    selected_mode = str(mode or "AM").strip().upper()
    if selected_mode not in SUPPORTED_MODES:
        raise ValueError(f"Unsupported transmission mode: {selected_mode}.")

    try:
        power = float(station_dbm)
        words_per_minute = int(cw_wpm)
    except (TypeError, ValueError) as exc:
        raise ValueError("Power and CW speed must be numeric values.") from exc
    if not math.isfinite(power) or not -140 <= power <= 53:
        raise ValueError("Station power must be between -140 and 53 dBm.")
    if not 5 <= words_per_minute <= 60:
        raise ValueError("CW speed must be between 5 and 60 WPM.")

    short_description = str(description or "").strip()[:180]
    return {
        "type": "config",
        "rfHz": parse_rf_frequency(frequency),
        "stationName": name,
        "stationDescription": short_description,
        "stationShortDescription": short_description,
        "stationLogoUrl": str(station_logo_url or "").strip()[:512],
        "stationDetails": str(station_details or "").strip()[:1_200],
        "stationPageLayout": str(station_page_layout or "")[:40_000],
        "discordAuthToken": auth_token,
        "mode": selected_mode,
        "sampleRate": SAMPLE_RATE_HZ,
        "stationDbm": power,
        "fadeMs": 15_000,
        "fadeMinDbm": 25,
        "fadeShape": 0,
        "allowOverlapVisitors": bool(allow_overlap),
        "cwText": str(cw_text or "CQ CQ DE TXITY").strip()[:300],
        "cwWpm": words_per_minute,
        "cwLoop": True,
        "agcEnabled": True,
        "agcTarget": 0.19,
        "micGainDb": 0,
        "bassDb": 0,
        "trebleDb": 0,
        "presenceDb": 0,
        "drive": 0.015,
        "ssbBandwidthHz": 10_000,
        "ssbLowCutHz": 325,
        "ssbHighCutHz": 0,
        "amBandwidthHz": 12_000,
        "amPreEmphasis": False,
    }


def list_audio_inputs() -> list[tuple[int, str]]:
    """Return available PortAudio input devices."""
    import sounddevice

    devices = sounddevice.query_devices()
    return [
        (index, str(device.get("name") or f"Audio input {index}"))
        for index, device in enumerate(devices)
        if int(device.get("max_input_channels", 0)) > 0
    ]


def _safe_error(error: BaseException) -> str:
    """Avoid including authenticated WebSocket query strings in error text."""
    text = str(error)
    text = re.sub(r"(?i)wss?://[^\s]+", "[WebSocket URL redacted]", text)
    text = re.sub(r"(?i)(authToken|sessionSecret)=([^&\s]+)", r"\1=[redacted]", text)
    return f"{type(error).__name__}: {text[:700]}"


class TxityTransmitter:
    """Stream microphone PCM to TXITY and send PTT/config over its control socket."""

    def __init__(self, client: Any, events: queue.Queue[dict[str, Any]]) -> None:
        self.client = client
        self.events = events
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._starting = False
        self._active = False
        self._ptt_sent = False
        self._audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=AUDIO_QUEUE_FRAMES)

    @property
    def is_busy(self) -> bool:
        with self._lock:
            return self._starting or self._active

    def start(self, config: dict[str, Any], input_device: int | None = None) -> None:
        with self._lock:
            if self._starting or self._active or (self._thread and self._thread.is_alive()):
                raise RuntimeError("A transmission is already starting or active.")
            self._stop_event.clear()
            self._audio_queue = queue.Queue(maxsize=AUDIO_QUEUE_FRAMES)
            self._starting = True
            self._active = False
            self._ptt_sent = False
            self._thread = threading.Thread(
                target=self._run,
                args=(dict(config), input_device),
                daemon=True,
                name="txity-transmitter",
            )
            self.events.put({
                "kind": "tx_state",
                "state": "starting",
                "text": "Preparing the TXITY transmission connection...",
            })
            self._thread.start()

    def stop(self, wait: float = 0.0) -> None:
        with self._lock:
            thread = self._thread
            busy = self._starting or self._active
            self._stop_event.set()
        if busy:
            self.events.put({
                "kind": "tx_state",
                "state": "stopping",
                "text": "Stopping transmission...",
            })
        if wait > 0 and thread and thread is not threading.current_thread():
            thread.join(timeout=wait)

    def _run(self, config: dict[str, Any], input_device: int | None) -> None:
        import websocket

        mode = str(config.get("mode", "AM")).upper()
        needs_audio = mode != "CW"
        audio_socket = None
        input_stream = None
        ptt_sent = False
        audio_thread_error: list[str] = []
        audio_queue = self._audio_queue
        send_count = 0
        drop_count = 0
        last_metrics_at = time.monotonic()
        carry = bytearray()
        last_callback_warning = 0.0

        def audio_callback(indata, _frames, _time_info, status) -> None:
            nonlocal drop_count, last_callback_warning
            if status:
                now = time.monotonic()
                if now - last_callback_warning >= 5.0:
                    last_callback_warning = now
                    self.events.put({
                        "kind": "tx_audio_warning",
                        "text": str(status)[:300],
                    })
            try:
                carry.extend(indata.tobytes())
                while len(carry) >= FRAME_BYTES:
                    frame = bytes(carry[:FRAME_BYTES])
                    del carry[:FRAME_BYTES]
                    try:
                        audio_queue.put_nowait(frame)
                    except queue.Full:
                        try:
                            audio_queue.get_nowait()
                        except queue.Empty:
                            pass
                        try:
                            audio_queue.put_nowait(frame)
                            drop_count += 1
                        except queue.Full:
                            drop_count += 1
            except Exception as exc:
                audio_thread_error[:] = [_safe_error(exc)]

        try:
            if self._stop_event.is_set():
                return
            session_id, session_secret, auth_token = self.client.transmission_credentials()
            if not self.client.control_connected():
                raise ConnectionError("TXITY control connection is not ready. Reconnect and try again.")

            if needs_audio:
                import sounddevice

                audio_url = build_audio_socket_url(session_id, session_secret, auth_token)
                audio_socket = websocket.create_connection(
                    audio_url,
                    timeout=10,
                    origin=BASE_URL,
                    enable_multithread=True,
                )
                audio_socket.settimeout(1.0)
                self.events.put({
                    "kind": "tx_log",
                    "text": "Authenticated TX audio WebSocket connected.",
                })

                input_stream = sounddevice.InputStream(
                    device=input_device,
                    samplerate=SAMPLE_RATE_HZ,
                    channels=1,
                    dtype="float32",
                    blocksize=BATCH_SAMPLES,
                    latency="low",
                    callback=audio_callback,
                )
                input_stream.start()
                self.events.put({
                    "kind": "tx_log",
                    "text": "Microphone opened at 48 kHz, mono Float32; sending 512-sample batches.",
                })

            if self._stop_event.is_set():
                return

            if self._stop_event.is_set():
                return
            self.client.send_control_message(config)
            if self._stop_event.is_set():
                return
            self.client.send_control_message({"type": "ptt", "ptt": True})
            ptt_sent = True
            with self._lock:
                self._ptt_sent = True
                self._active = True
                self._starting = False
            self.events.put({
                "kind": "tx_state",
                "state": "transmitting",
                "text": "Transmitting CW control" if not needs_audio else "Transmitting microphone audio to TXITY.",
            })

            while not self._stop_event.is_set():
                if not self.client.control_connected():
                    raise ConnectionError("TXITY control connection was lost during transmission.")
                if audio_thread_error:
                    raise RuntimeError(audio_thread_error[0])

                if not needs_audio:
                    self._stop_event.wait(0.2)
                    continue

                try:
                    frame = audio_queue.get(timeout=0.2)
                except queue.Empty:
                    continue

                audio_socket.send(frame, opcode=websocket.ABNF.OPCODE_BINARY)
                send_count += 1
                now = time.monotonic()
                if now - last_metrics_at >= 1.0:
                    self.events.put({
                        "kind": "tx_metrics",
                        "frames_sent": send_count,
                        "frames_dropped": drop_count,
                        "queue_depth": audio_queue.qsize(),
                    })
                    last_metrics_at = now

        except Exception as exc:
            self.events.put({"kind": "tx_error", "text": _safe_error(exc)})
        finally:
            if ptt_sent:
                try:
                    self.client.send_control_message({"type": "ptt", "ptt": False})
                except Exception:
                    pass

            if input_stream is not None:
                try:
                    input_stream.stop()
                except Exception:
                    pass
                try:
                    input_stream.close()
                except Exception:
                    pass

            if audio_socket is not None:
                try:
                    audio_socket.close()
                except Exception:
                    pass

            with self._lock:
                self._active = False
                self._starting = False
                self._ptt_sent = False
                self._thread = None

            self.events.put({
                "kind": "tx_state",
                "state": "stopped",
                "text": "Transmission stopped.",
            })
