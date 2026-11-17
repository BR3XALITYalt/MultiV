"""Streaming FreeDV RADE V1 encoding and its verified runtime package."""

from __future__ import annotations

import hashlib
import os
import queue
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import zipfile
from collections import deque
from pathlib import Path
from typing import Callable
from urllib.request import Request, urlopen

import numpy as np


RADE_SPEECH_SAMPLE_RATE = 16_000
RADE_MODEM_SAMPLE_RATE = 8_000
RADE_AUDIO_CENTER_HZ = 1_500
RADE_CHANNEL_BANDWIDTH_HZ = 1_500
RADE_INPUT_FRAME_SAMPLES = 160
RADE_PREROLL_FRAMES = 20
RADE_AUDIO_QUEUE_FRAMES = 256

_RUNTIME_TAG = "txitpy-rade-runtime"
_RUNTIME_NAME = "txitpy-rade-v1-windows-x64.zip"
_RUNTIME_BASE_URL = (
    "https://github.com/BR3XALITYalt/TXITPY/releases/download/"
    f"{_RUNTIME_TAG}"
)
_MAX_RUNTIME_BYTES = 64 * 1024 * 1024


def _runtime_cache_root() -> Path:
    if os.name == "nt":
        base = Path(
            os.environ.get("LOCALAPPDATA")
            or (Path.home() / "AppData" / "Local")
        )
    else:
        base = Path(
            os.environ.get("XDG_CACHE_HOME")
            or (Path.home() / ".cache")
        )
    return base / "TXITPY" / "rade-v1"


def _download_asset(url: str, limit: int) -> bytes:
    request = Request(url, headers={"User-Agent": "TXITPY/FreeDV-RADE"})
    with urlopen(request, timeout=60) as response:
        data = response.read(limit + 1)
    if not data or len(data) > limit:
        raise RuntimeError("The FreeDV RADE runtime download was empty or too large.")
    return data


def prepare_rade_encoder(
    on_log: Callable[[str], None] | None = None,
) -> Path:
    """Download and verify the Windows x64 FreeDV RADE V1 encoder package."""
    if os.name != "nt":
        raise RuntimeError(
            "The prebuilt FreeDV RADE V1 runtime is currently available for Windows x64."
        )
    if struct.calcsize("P") != 8:
        raise RuntimeError("FreeDV RADE V1 requires 64-bit Windows.")

    root = _runtime_cache_root()
    install_dir = root / "runtime"
    executable = install_dir / "freedv_rade_tx.exe"
    required_files = (
        executable,
        install_dir / "librade.dll",
        install_dir / "libgcc_s_seh-1.dll",
        install_dir / "libwinpthread-1.dll",
        install_dir / "libssp-0.dll",
    )
    if all(path.is_file() for path in required_files):
        return executable

    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".install.lock"
    with _runtime_install_lock:
        if all(path.is_file() for path in required_files):
            return executable

        if on_log:
            on_log("Downloading the FreeDV RADE V1 encoder package.")

        archive_url = f"{_RUNTIME_BASE_URL}/{_RUNTIME_NAME}"
        checksum_url = f"{archive_url}.sha256"
        archive_data = _download_asset(archive_url, _MAX_RUNTIME_BYTES)
        checksum_data = _download_asset(checksum_url, 4_096).decode(
            "ascii",
            errors="strict",
        )
        expected_hash = checksum_data.strip().split()[0].casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise RuntimeError("The FreeDV RADE runtime checksum file is invalid.")
        if hashlib.sha256(archive_data).hexdigest() != expected_hash:
            raise RuntimeError(
                "The FreeDV RADE runtime checksum did not match. The package was not installed."
            )

        temporary = Path(tempfile.mkdtemp(prefix=".rade-install-", dir=root))
        try:
            extracted = temporary / "runtime"
            extracted.mkdir()
            with zipfile.ZipFile(__import__("io").BytesIO(archive_data)) as archive:
                for member in archive.infolist():
                    member_path = Path(member.filename)
                    if member_path.is_absolute() or ".." in member_path.parts:
                        raise RuntimeError("The FreeDV RADE runtime contains an unsafe file path.")
                    destination = (extracted / member_path).resolve()
                    if extracted.resolve() not in destination.parents and destination != extracted.resolve():
                        raise RuntimeError("The FreeDV RADE runtime contains an unsafe file path.")
                archive.extractall(extracted)

            missing = [
                path.name for path in required_files
                if not (extracted / path.name).is_file()
            ]
            if missing:
                raise RuntimeError(
                    "The FreeDV RADE runtime package is incomplete: "
                    + ", ".join(missing)
                    + "."
                )

            if install_dir.exists():
                shutil.rmtree(install_dir)
            extracted.replace(install_dir)
        finally:
            shutil.rmtree(temporary, ignore_errors=True)

        if on_log:
            on_log("FreeDV RADE V1 is ready.")
        return executable


_runtime_install_lock = threading.Lock()


class StreamingDecimator3:
    """Band-limit 48 kHz input and decimate it to 16 kHz without resetting filter state."""

    def __init__(self) -> None:
        taps = 63
        centre = (taps - 1) / 2
        cutoff = 7_200.0 / 48_000.0
        indices = np.arange(taps, dtype=np.float64) - centre
        coefficients = 2.0 * cutoff * np.sinc(2.0 * cutoff * indices)
        coefficients *= np.hamming(taps)
        coefficients /= np.sum(coefficients)
        self._coefficients = coefficients
        self._history = np.zeros(taps - 1, dtype=np.float64)
        self._phase = 0

    def process(self, samples: np.ndarray) -> np.ndarray:
        source = np.asarray(samples, dtype=np.float64).reshape(-1)
        if source.size == 0:
            return np.empty(0, dtype=np.float32)

        source = np.nan_to_num(source, nan=0.0, posinf=0.0, neginf=0.0)
        combined = np.concatenate((self._history, source))
        filtered = np.convolve(combined, self._coefficients, mode="valid")
        self._history = combined[-self._history.size:].copy()

        indices = np.arange(self._phase, filtered.size, 3, dtype=np.int64)
        result = filtered[indices].astype(np.float32)
        self._phase = (self._phase + indices.size * 3 - filtered.size) % 3
        return result


class StreamingLinearResampler:
    """Resample mono audio while preserving interpolation state across blocks."""

    def __init__(self, source_rate: int, target_rate: int) -> None:
        if source_rate <= 0 or target_rate <= 0:
            raise ValueError("Audio sample rates must be positive.")
        self.step = source_rate / target_rate
        self._buffer: np.ndarray | None = None
        self._position = 0.0

    def process(self, samples: np.ndarray) -> np.ndarray:
        chunk = np.asarray(samples, dtype=np.float32).reshape(-1)
        if chunk.size == 0:
            return np.empty(0, dtype=np.float32)

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


def _read_exact(stream, byte_count: int) -> bytes:
    parts: list[bytes] = []
    remaining = byte_count
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            break
        parts.append(chunk)
        remaining -= len(chunk)
    return b"".join(parts)


class FreeDVRadeEncoder:
    """Stream 48 kHz mono speech through the RADE V1 encoder helper."""

    def __init__(
        self,
        executable: str | Path,
        on_audio: Callable[[np.ndarray], None],
    ) -> None:
        self.executable = Path(executable).resolve()
        self.on_audio = on_audio
        if not self.executable.is_file():
            raise FileNotFoundError(f"FreeDV RADE encoder not found: {self.executable}")

        environment = os.environ.copy()
        environment["PATH"] = (
            str(self.executable.parent)
            + os.pathsep
            + environment.get("PATH", "")
        )
        try:
            self._process = subprocess.Popen(
                [str(self.executable)],
                cwd=str(self.executable.parent),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                bufsize=0,
            )
        except OSError as exc:
            raise RuntimeError(f"Could not start the FreeDV RADE encoder: {exc}") from exc

        self._input_queue: queue.Queue[bytes | None] = queue.Queue(maxsize=64)
        self._input_lock = threading.Lock()
        self._speech_buffer = np.empty(0, dtype=np.float32)
        self._decimator = StreamingDecimator3()
        self._upsampler = StreamingLinearResampler(
            RADE_MODEM_SAMPLE_RATE,
            48_000,
        )
        self._accepting = True
        self._finished = False
        self._finish_lock = threading.Lock()
        self._error_lock = threading.Lock()
        self._error_text = ""
        self._stderr_lines: deque[str] = deque(maxlen=40)

        self._stderr_thread = threading.Thread(
            target=self._read_stderr,
            name="txitpy-rade-stderr",
            daemon=True,
        )
        self._reader_thread = threading.Thread(
            target=self._read_waveform,
            name="txitpy-rade-waveform",
            daemon=True,
        )
        self._writer_thread = threading.Thread(
            target=self._write_speech,
            name="txitpy-rade-speech",
            daemon=True,
        )
        self._stderr_thread.start()
        self._reader_thread.start()
        self._writer_thread.start()

    @property
    def error(self) -> str:
        with self._error_lock:
            return self._error_text

    def _set_error(self, message: str) -> None:
        with self._error_lock:
            if not self._error_text:
                tail = "\n".join(self._stderr_lines).strip()
                self._error_text = message + (f"\n{tail}" if tail else "")

    def _read_stderr(self) -> None:
        stream = self._process.stderr
        if stream is None:
            return
        try:
            for raw_line in iter(stream.readline, b""):
                line = raw_line.decode("utf-8", errors="replace").strip()
                if line:
                    self._stderr_lines.append(line[:300])
        except OSError:
            pass

    def _write_speech(self) -> None:
        stream = self._process.stdin
        try:
            while True:
                frame = self._input_queue.get()
                if frame is None:
                    break
                if stream is None:
                    raise BrokenPipeError("The RADE encoder input pipe is unavailable.")
                view = memoryview(frame)
                position = 0
                while position < len(view):
                    written = stream.write(view[position:])
                    if not written:
                        raise BrokenPipeError("The RADE encoder closed its input pipe.")
                    position += written
        except (BrokenPipeError, OSError, ValueError) as exc:
            self._set_error(f"FreeDV RADE audio input failed: {exc}")
        finally:
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def _read_waveform(self) -> None:
        stream = self._process.stdout
        if stream is None:
            self._set_error("The FreeDV RADE waveform output pipe is unavailable.")
            return
        try:
            while True:
                header = _read_exact(stream, 4)
                if not header:
                    break
                if len(header) != 4:
                    raise RuntimeError("The RADE encoder returned an incomplete audio block header.")
                sample_count = struct.unpack("<I", header)[0]
                if not 0 < sample_count <= 8_192:
                    raise RuntimeError("The RADE encoder returned an invalid audio block size.")
                payload = _read_exact(stream, sample_count * 4)
                if len(payload) != sample_count * 4:
                    raise RuntimeError("The RADE encoder returned an incomplete audio block.")
                waveform = np.frombuffer(payload, dtype="<f4").astype(np.float32, copy=True)
                if not np.isfinite(waveform).all():
                    raise RuntimeError("The RADE encoder returned non-finite audio samples.")
                output = self._upsampler.process(waveform)
                if output.size:
                    self.on_audio(output)
        except Exception as exc:
            self._set_error(f"FreeDV RADE waveform processing failed: {exc}")

    def feed(self, samples: np.ndarray) -> None:
        source = np.asarray(samples, dtype=np.float32).reshape(-1)
        if source.size == 0:
            return
        with self._input_lock:
            if not self._accepting:
                raise RuntimeError("The FreeDV RADE encoder is stopping.")
            if self._process.poll() is not None:
                raise RuntimeError(
                    self.error or "The FreeDV RADE encoder stopped unexpectedly."
                )

            converted = self._decimator.process(source)
            if converted.size:
                self._speech_buffer = np.concatenate((self._speech_buffer, converted))

            while self._speech_buffer.size >= RADE_INPUT_FRAME_SAMPLES:
                frame = self._speech_buffer[:RADE_INPUT_FRAME_SAMPLES]
                self._speech_buffer = self._speech_buffer[RADE_INPUT_FRAME_SAMPLES:]
                pcm = np.clip(
                    np.rint(frame.astype(np.float64) * 32768.0),
                    -32767,
                    32767,
                ).astype("<i2", copy=False)
                try:
                    self._input_queue.put(pcm.tobytes(), timeout=0.15)
                except queue.Full as exc:
                    message = (
                        "FreeDV RADE cannot encode the incoming audio in real time. "
                        "The computer may not have enough CPU capacity."
                    )
                    self._set_error(message)
                    raise RuntimeError(message) from exc

    def finish(self, timeout: float = 12.0) -> None:
        with self._finish_lock:
            if self._finished:
                return
            with self._input_lock:
                self._accepting = False
                if self._speech_buffer.size:
                    padded = np.zeros(RADE_INPUT_FRAME_SAMPLES, dtype=np.float32)
                    padded[:self._speech_buffer.size] = self._speech_buffer
                    pcm = np.clip(
                        np.rint(padded.astype(np.float64) * 32768.0),
                        -32767,
                        32767,
                    ).astype("<i2", copy=False)
                    try:
                        self._input_queue.put(pcm.tobytes(), timeout=1.0)
                    except queue.Full:
                        self._set_error("FreeDV RADE could not flush its final speech frame.")
                try:
                    self._input_queue.put(None, timeout=1.0)
                except queue.Full:
                    self._set_error("FreeDV RADE did not drain its speech input queue.")
                    try:
                        self._process.terminate()
                    except OSError:
                        pass

            self._writer_thread.join(timeout=timeout)
            if self._writer_thread.is_alive():
                self._set_error("FreeDV RADE did not finish writing its speech input.")
                self._terminate_process()

            self._reader_thread.join(timeout=timeout)
            if self._reader_thread.is_alive():
                self._set_error("FreeDV RADE did not finish its final modem frame.")
                self._terminate_process()
                self._reader_thread.join(timeout=2.0)

            try:
                return_code = self._process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self._terminate_process()
                return_code = self._process.poll()

            self._stderr_thread.join(timeout=1.0)
            self._finished = True
            if return_code not in (0, None):
                self._set_error(f"FreeDV RADE encoder exited with code {return_code}.")

    def _terminate_process(self) -> None:
        try:
            self._process.terminate()
            self._process.wait(timeout=2.0)
        except (OSError, subprocess.TimeoutExpired):
            try:
                self._process.kill()
            except OSError:
                pass

    def close(self) -> None:
        self.finish()
