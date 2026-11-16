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
PTT_CONFIRMATION_GRACE_SECONDS = 4.0
SUPPORTED_MODES = ("AM", "USB", "LSB", "USB+C", "LSB+C", "DSB", "CW")
DEFAULT_MODE_BANDWIDTH_HZ = {
    "AM": 12_000,
    "DSB": 12_000,
    "USB": 10_000,
    "LSB": 10_000,
    "USB+C": 10_000,
    "LSB+C": 10_000,
    "CW": 500,
}
SUPPORTED_AUDIO_SOURCES = ("mic", "loopback")


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


def mode_bandwidth_hz(mode: Any, station: dict[str, Any] | None = None) -> int:
    """Return a TXITY channel-width estimate, preferring station-reported values."""
    station = station if isinstance(station, dict) else {}
    mode_name = str(mode or "AM").strip().upper()
    field_names = ["bandwidthHz", "occupiedBandwidthHz", "rfBandwidthHz"]
    if mode_name in {"AM", "DSB"}:
        field_names.append("amBandwidthHz")
    elif mode_name in {"USB", "LSB", "USB+C", "LSB+C"}:
        field_names.append("ssbBandwidthHz")
    elif mode_name == "CW":
        field_names.append("cwBandwidthHz")

    for field_name in field_names:
        candidate = station.get(field_name)
        if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
            if math.isfinite(float(candidate)) and 0 < float(candidate) <= 2_000_000:
                return int(round(candidate))
    return DEFAULT_MODE_BANDWIDTH_HZ.get(mode_name, DEFAULT_MODE_BANDWIDTH_HZ["AM"])


def find_frequency_overlaps(
    frequency: Any,
    mode: Any,
    stations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Find estimated channel overlaps with the supplied TXITY station records."""
    target_hz = parse_rf_frequency(frequency)
    target_width = mode_bandwidth_hz(mode)
    target_low = target_hz - target_width / 2
    target_high = target_hz + target_width / 2
    overlaps: list[dict[str, Any]] = []

    for station in stations:
        if not isinstance(station, dict):
            continue
        other_hz = None
        for field_name in ("frequency_hz", "frequencyHz", "rfHz"):
            candidate = station.get(field_name)
            if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
                if math.isfinite(float(candidate)) and candidate > 0:
                    other_hz = int(round(candidate))
                    break
        if other_hz is None:
            candidate = station.get("frequency")
            try:
                other_hz = parse_rf_frequency(candidate)
            except (TypeError, ValueError):
                continue

        other_mode = str(station.get("mode") or "AM").strip().upper()
        other_width = mode_bandwidth_hz(other_mode, station)
        other_low = other_hz - other_width / 2
        other_high = other_hz + other_width / 2
        overlap_width = min(target_high, other_high) - max(target_low, other_low)
        if overlap_width <= 0:
            continue

        overlaps.append({
            "station_id": str(station.get("id") or station.get("station_id") or ""),
            "station_name": str(station.get("station") or station.get("name") or "Unknown station"),
            "frequency_hz": other_hz,
            "mode": other_mode,
            "bandwidth_hz": other_width,
            "offset_hz": abs(target_hz - other_hz),
            "overlap_hz": int(round(overlap_width)),
            "allow_overlap_visitors": station.get("allow_overlap_visitors", station.get("allowOverlapVisitors"))
                if isinstance(station.get("allow_overlap_visitors", station.get("allowOverlapVisitors")), bool)
                else None,
        })

    return sorted(overlaps, key=lambda item: (item["offset_hz"], item["station_name"].casefold()))


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
    cw_loop: bool = True,
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
        "cwLoop": bool(cw_loop),
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
    """Return available PortAudio microphone and line-input devices."""
    import sounddevice

    devices = sounddevice.query_devices()
    return [
        (index, str(device.get("name") or f"Audio input {index}"))
        for index, device in enumerate(devices)
        if int(device.get("max_input_channels", 0)) > 0
    ]


def list_loopback_devices() -> list[tuple[int, str]]:
    """Return Windows WASAPI loopback devices for capturing desktop playback."""
    try:
        import pyaudiowpatch as pyaudio
    except ImportError as exc:
        raise RuntimeError(
            "Desktop audio capture requires PyAudioWPatch. Install the project requirements and reopen TXITPY."
        ) from exc

    manager = pyaudio.PyAudio()
    try:
        try:
            default_info = manager.get_default_wasapi_loopback()
            default_index = int(default_info["index"])
        except (OSError, KeyError, TypeError, ValueError):
            default_index = None

        devices: list[tuple[int, str]] = []
        for info in manager.get_loopback_device_info_generator():
            try:
                index = int(info["index"])
                name = str(info.get("name") or f"Desktop output {index}")
            except (KeyError, TypeError, ValueError):
                continue
            label = f"{index}: {name}"
            if index == default_index:
                label += " (Default output)"
            devices.append((index, label))

        if default_index is not None:
            devices.sort(key=lambda item: (item[0] != default_index, item[0]))
        if not devices:
            raise RuntimeError(
                "No Windows WASAPI loopback devices were found. Check that a playback device is enabled."
            )
        return devices
    finally:
        manager.terminate()


def _safe_error(error: BaseException) -> str:
    """Avoid including authenticated WebSocket query strings in error text."""
    text = str(error)
    text = re.sub(r"(?i)wss?://[^\s]+", "[WebSocket URL redacted]", text)
    text = re.sub(r"(?i)(authToken|sessionSecret)=([^&\s]+)", r"\1=[redacted]", text)
    return f"{type(error).__name__}: {text[:700]}"


class StreamingLinearResampler:
    """Resample mono float audio while retaining phase between callback blocks."""

    def __init__(self, source_rate: int, target_rate: int = SAMPLE_RATE_HZ) -> None:
        if source_rate <= 0 or target_rate <= 0:
            raise ValueError("Audio sample rates must be positive.")
        self.source_rate = int(source_rate)
        self.target_rate = int(target_rate)
        self.step = self.source_rate / self.target_rate
        self._buffer = None
        self._position = 0.0

    def process(self, samples):
        import numpy as np

        chunk = np.asarray(samples, dtype=np.float32).reshape(-1)
        if chunk.size == 0:
            return np.empty(0, dtype=np.float32)
        if self.source_rate == self.target_rate:
            return chunk.copy()

        combined = (
            chunk
            if self._buffer is None
            else np.concatenate((self._buffer, chunk))
        )
        if combined.size < 2:
            self._buffer = combined.copy()
            return np.empty(0, dtype=np.float32)

        positions = np.arange(
            self._position,
            combined.size - 1,
            self.step,
            dtype=np.float64,
        )
        if positions.size == 0:
            self._buffer = combined.copy()
            return np.empty(0, dtype=np.float32)

        left = positions.astype(np.int64)
        fraction = (positions - left).astype(np.float32)
        output = combined[left] + (
            combined[left + 1] - combined[left]
        ) * fraction

        next_position = self._position + positions.size * self.step
        discard = max(0, min(int(next_position) - 1, combined.size - 1))
        self._buffer = combined[discard:].copy()
        self._position = next_position - discard
        return np.asarray(output, dtype=np.float32)


class TxityTransmitter:
    """Stream microphone or desktop-loopback PCM to TXITY."""

    def __init__(self, client: Any, events: queue.Queue[dict[str, Any]]) -> None:
        self.client = client
        self.events = events
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._starting = False
        self._active = False
        self._ptt_sent = False
        self._ptt_requested_at: float | None = None
        self._audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=AUDIO_QUEUE_FRAMES)

    @property
    def is_busy(self) -> bool:
        with self._lock:
            return self._starting or self._active

    @property
    def server_rejection_ready(self) -> bool:
        """Ignore stale negative status snapshots immediately after sending PTT."""
        with self._lock:
            requested_at = self._ptt_requested_at
            return bool(
                self._active
                and self._ptt_sent
                and not self._stop_event.is_set()
                and requested_at is not None
                and time.monotonic() - requested_at >= PTT_CONFIRMATION_GRACE_SECONDS
            )

    def start(
        self,
        config: dict[str, Any],
        input_device: int | None = None,
        audio_source: str = "mic",
    ) -> None:
        selected_source = str(audio_source or "mic").casefold()
        if selected_source not in SUPPORTED_AUDIO_SOURCES:
            raise ValueError("Choose microphone input or desktop audio loopback.")

        with self._lock:
            if self._starting or self._active or (self._thread and self._thread.is_alive()):
                raise RuntimeError("A transmission is already starting or active.")
            self._stop_event.clear()
            self._audio_queue = queue.Queue(maxsize=AUDIO_QUEUE_FRAMES)
            self._starting = True
            self._active = False
            self._ptt_sent = False
            self._ptt_requested_at = None
            self._thread = threading.Thread(
                target=self._run,
                args=(dict(config), input_device, selected_source),
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

    def _run(
        self,
        config: dict[str, Any],
        input_device: int | None,
        audio_source: str,
    ) -> None:
        import websocket

        mode = str(config.get("mode", "AM")).upper()
        needs_audio = mode != "CW"
        audio_socket = None
        input_stream = None
        loopback_stream = None
        loopback_manager = None
        ptt_sent = False
        audio_thread_error: list[str] = []
        audio_queue = self._audio_queue
        send_count = 0
        drop_count = 0
        last_metrics_at = time.monotonic()
        carry = bytearray()
        last_callback_warning = 0.0

        def enqueue_pcm_samples(samples) -> None:
            nonlocal drop_count
            import numpy as np

            pcm = np.asarray(samples, dtype="<f4").reshape(-1)
            if pcm.size == 0:
                return
            carry.extend(pcm.tobytes())
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

        def report_audio_warning(status: Any) -> None:
            nonlocal last_callback_warning
            if not status:
                return
            now = time.monotonic()
            if now - last_callback_warning >= 5.0:
                last_callback_warning = now
                self.events.put({
                    "kind": "tx_audio_warning",
                    "text": str(status)[:300],
                })

        def microphone_callback(indata, _frames, _time_info, status) -> None:
            report_audio_warning(status)
            try:
                enqueue_pcm_samples(indata[:, 0])
            except Exception as exc:
                audio_thread_error[:] = [_safe_error(exc)]

        def loopback_callback_factory(channels: int, source_rate: int, pyaudio):
            import numpy as np

            resampler = StreamingLinearResampler(source_rate, SAMPLE_RATE_HZ)

            def callback(in_data, _frame_count, _time_info, status):
                report_audio_warning(status)
                try:
                    raw = np.frombuffer(in_data or b"", dtype=np.float32)
                    if raw.size:
                        usable = raw.size - (raw.size % channels)
                        if usable <= 0:
                            return (in_data, pyaudio.paContinue)
                        audio = raw[:usable].reshape(-1, channels)
                        mono = audio.mean(axis=1, dtype=np.float32) if channels > 1 else audio[:, 0]
                        enqueue_pcm_samples(resampler.process(mono))
                except Exception as exc:
                    audio_thread_error[:] = [_safe_error(exc)]
                return (None, pyaudio.paContinue)

            return callback

        try:
            if self._stop_event.is_set():
                return
            session_id, session_secret, auth_token = self.client.transmission_credentials()
            if not self.client.control_connected():
                raise ConnectionError("TXITY control connection is not ready. Reconnect and try again.")

            if needs_audio:
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

                if audio_source == "loopback":
                    try:
                        import pyaudiowpatch as pyaudio
                    except ImportError as exc:
                        raise RuntimeError(
                            "Desktop audio capture requires PyAudioWPatch. Install the project requirements and reopen TXITPY."
                        ) from exc

                    if input_device is None:
                        raise RuntimeError("Select a desktop output device for loopback capture.")

                    loopback_manager = pyaudio.PyAudio()
                    info = loopback_manager.get_device_info_by_index(input_device)
                    if not bool(info.get("isLoopbackDevice")):
                        raise RuntimeError("The selected device is not a Windows WASAPI loopback input.")

                    channels = min(2, int(info.get("maxInputChannels", 0)))
                    source_rate = int(round(float(info.get("defaultSampleRate", SAMPLE_RATE_HZ))))
                    if channels < 1 or source_rate < 1:
                        raise RuntimeError("The selected desktop output does not expose a usable audio format.")

                    loopback_stream = loopback_manager.open(
                        format=pyaudio.paFloat32,
                        channels=channels,
                        rate=source_rate,
                        input=True,
                        input_device_index=input_device,
                        frames_per_buffer=BATCH_SAMPLES,
                        stream_callback=loopback_callback_factory(channels, source_rate, pyaudio),
                        start=False,
                    )
                    loopback_stream.start_stream()
                    self.events.put({
                        "kind": "tx_log",
                        "text": (
                            f"Desktop output loopback opened: {info.get('name', 'selected output')} "
                            f"({source_rate} Hz, {channels} channels); downmixing and converting to "
                            "48 kHz mono Float32."
                        ),
                    })
                else:
                    import sounddevice

                    input_stream = sounddevice.InputStream(
                        device=input_device,
                        samplerate=SAMPLE_RATE_HZ,
                        channels=1,
                        dtype="float32",
                        blocksize=BATCH_SAMPLES,
                        latency="low",
                        callback=microphone_callback,
                    )
                    input_stream.start()
                    self.events.put({
                        "kind": "tx_log",
                        "text": "Microphone opened at 48 kHz, mono Float32; sending 512-sample batches.",
                    })

            if self._stop_event.is_set():
                return

            self.client.send_control_message(config)
            if self._stop_event.is_set():
                return
            self.client.send_control_message({"type": "ptt", "ptt": True})
            ptt_sent = True
            with self._lock:
                self._ptt_sent = True
                self._ptt_requested_at = time.monotonic()
                self._active = True
                self._starting = False
            source_name = "desktop audio" if audio_source == "loopback" else "microphone audio"
            self.events.put({
                "kind": "tx_state",
                "state": "transmitting",
                "text": "Transmitting CW control" if not needs_audio else f"Transmitting {source_name} to TXITY.",
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

            if loopback_stream is not None:
                try:
                    if loopback_stream.is_active():
                        loopback_stream.stop_stream()
                except Exception:
                    pass
                try:
                    loopback_stream.close()
                except Exception:
                    pass

            if loopback_manager is not None:
                try:
                    loopback_manager.terminate()
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
                self._ptt_requested_at = None
                self._thread = None

            self.events.put({
                "kind": "tx_state",
                "state": "stopped",
                "text": "Transmission stopped.",
            })
