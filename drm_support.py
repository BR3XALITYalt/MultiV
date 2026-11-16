"""DRM30 encoder setup and bounded local playlist input for TXITPY."""

from __future__ import annotations

import json
import os
import random
import re
import secrets
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import quote, urlsplit

from audio_playlist import build_playlist_order

DECDRM_REPOSITORY = "https://github.com/CasualArclamp/DecDRM.git"
DECDRM_COMMIT = "05845c9a754309b86d4985b25b1c3a787b988104"
DECDRM_VERSION = "0.7.1"
DRM_PLAYLIST_EXTENSIONS = frozenset({".flac", ".mp3", ".ogg"})


def _cache_root() -> Path:
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or Path.home())
        return root / "TXITPY" / "DRM"
    root = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    return root / "txitpy" / "drm"


def _run_command(
    command: list[str],
    *,
    cwd: Path | None = None,
    on_log: Callable[[str], None] | None = None,
) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"Required build tool was not found: {command[0]}") from exc
    output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
    if result.returncode != 0:
        tail = "\n".join(output.splitlines()[-50:])
        raise RuntimeError(
            f"Command failed ({result.returncode}): {' '.join(command)}"
            + (f"\n{tail}" if tail else "")
        )
    if on_log and output:
        for line in output.splitlines()[-12:]:
            if line.strip():
                on_log(line.strip()[:500])
    return output


def _patch_decdrm_output_validation(plan_file: Path) -> bool:
    """Allow TXITPY to consume returned samples without an extra output sink."""
    plan_source = plan_file.read_text(encoding="utf-8")
    output_check = (
        '    if out.file.is_none() && out.device.is_none() {\\n'
        '        p.push("output: set ' + chr(96) + 'file' + chr(96) + ' and/or ' + chr(96) + 'device' + chr(96) + '");\\n'
        '    }'
    )
    marker = "TXITPY consumes the samples returned by Station::transmit_frame()"
    if output_check in plan_source:
        plan_source = plan_source.replace(output_check, "    // " + marker + "\\n", 1)
        plan_file.write_text(plan_source, encoding="utf-8")
        return True
    if marker in plan_source:
        return False
    raise RuntimeError(
        "The pinned DecDRM source did not match the expected output validation code. "
        "TXITPY cannot safely prepare its in-memory output mode."
    )


def ensure_drm_encoder(on_log: Callable[[str], None] | None = None) -> Path:
    """Build the pinned DecDRM bridge once and return its executable path."""
    required_tools = ("git", "cargo", "rustc", "cmake")
    missing_tools = [tool for tool in required_tools if shutil.which(tool) is None]
    compiler_names = ("cl", "clang-cl") if os.name == "nt" else ("c++", "g++", "clang++")
    if not any(shutil.which(name) for name in compiler_names):
        missing_tools.append("a C/C++ compiler")
    if missing_tools:
        raise RuntimeError(
            "DRM mode needs Git, Rust 1.88 or newer, CMake, and a C/C++ compiler. "
            "Missing: " + ", ".join(missing_tools) + "."
        )

    rust_version = _run_command(["rustc", "--version"]).strip()
    match = re.search(r"rustc (\\d+)\\.(\\d+)", rust_version)
    if not match or (int(match.group(1)), int(match.group(2))) < (1, 88):
        raise RuntimeError(
            f"DRM mode needs Rust 1.88 or newer; found: {rust_version or 'unknown version'}."
        )

    cache = _cache_root()
    cache.mkdir(parents=True, exist_ok=True)
    upstream = cache / f"DecDRM-{DECDRM_COMMIT[:12]}"

    if not (upstream / ".git").is_dir():
        if upstream.exists():
            shutil.rmtree(upstream, ignore_errors=True)
        if on_log:
            on_log(f"Downloading DecDRM {DECDRM_VERSION} source and codec dependencies.")
        _run_command(
            ["git", "clone", "--depth", "1", "--branch", "main", DECDRM_REPOSITORY, str(upstream)],
            on_log=on_log,
        )

    current = _run_command(["git", "rev-parse", "HEAD"], cwd=upstream).strip()
    if current != DECDRM_COMMIT:
        _run_command(["git", "fetch", "--depth", "1", "origin", DECDRM_COMMIT], cwd=upstream, on_log=on_log)
        _run_command(["git", "checkout", "--detach", DECDRM_COMMIT], cwd=upstream, on_log=on_log)

    _run_command(
        ["git", "submodule", "update", "--init", "--depth", "1", "--recursive"],
        cwd=upstream,
        on_log=on_log,
    )

    plan_file = upstream / "crates" / "decdrm-station" / "src" / "plan.rs"
    output_validation_patched = _patch_decdrm_output_validation(plan_file)

    package_dir = upstream / "apps" / "txitpy-drm-bridge"
    (package_dir / "src").mkdir(parents=True, exist_ok=True)
    local_bridge_source = Path(__file__).resolve().parent / "drm_encoder" / "src" / "main.rs"
    if not local_bridge_source.is_file():
        raise RuntimeError("The TXITPY DRM encoder source file is missing.")

    manifest = '''[package]
name = "txitpy-drm-bridge"
version = "0.1.0"
edition.workspace = true
license.workspace = true
repository.workspace = true
rust-version.workspace = true

[dependencies]
decdrm-station = { path = "../../crates/decdrm-station" }
'''
    manifest_path = package_dir / "Cargo.toml"
    source_path = package_dir / "src" / "main.rs"
    source_text = local_bridge_source.read_text(encoding="utf-8")
    changed = (
        output_validation_patched
        or not manifest_path.exists()
        or manifest_path.read_text(encoding="utf-8") != manifest
        or not source_path.exists()
        or source_path.read_text(encoding="utf-8") != source_text
    )
    if changed:
        manifest_path.write_text(manifest, encoding="utf-8")
        source_path.write_text(source_text, encoding="utf-8")

    executable = upstream / "target" / "release" / (
        "txitpy-drm-bridge.exe" if os.name == "nt" else "txitpy-drm-bridge"
    )
    if changed or not executable.is_file():
        if on_log:
            on_log("Building the DRM encoder. The first build also compiles its codec libraries.")
        _run_command(
            ["cargo", "build", "--release", "-p", "txitpy-drm-bridge"],
            cwd=upstream,
            on_log=on_log,
        )

    if not executable.is_file():
        raise RuntimeError("The DRM encoder build completed without producing its executable.")
    return executable


def _toml_string(value: str) -> str:
    """Encode a string as a TOML basic string."""
    return json.dumps(str(value), ensure_ascii=False)


def write_drm_station_config(
    path: str | Path,
    *,
    station_name: str,
    input_device_name: str | None = None,
    playlist_url: str | None = None,
) -> Path:
    """Write a one-service, 4.5 kHz DRM30 station configuration."""
    if bool(input_device_name) == bool(playlist_url):
        raise ValueError("Choose exactly one DRM audio input: microphone or playlist.")

    label = "".join(character for character in str(station_name or "TXITPY") if character.isprintable())
    label = label.strip()[:16] or "TXITPY"
    lines = [
        "[channel]",
        'mode = "B"',
        "occupancy = 0",
        'msc_mode = "16-QAM"',
        'sdc_mode = "4-QAM"',
        'interleaving = "short"',
        "protection_b = 0",
        "protection_a = 0",
        "",
        "[output]",
        'sample_format = "float32"',
        'format = "real"',
        "if_hz = 3000.0",
        "level_dbfs = -15.0",
        "band_limit = true",
        "",
        "[[service]]",
        f"label = {_toml_string(label)}",
        "id = 0xB30001",
        'language = "English"',
        'programme_type = "Pop Music"',
        "",
        "[service.audio]",
        'codec = "he-aac"',
        "core_rate = 12000",
        "stereo = false",
        'part = "B"',
        "share = 1.0",
        "",
        "[service.audio.input]",
    ]
    if playlist_url:
        lines.extend((f"url = {_toml_string(playlist_url)}", "stream_titles = false"))
    else:
        lines.append(f"device = {_toml_string(input_device_name or 'default')}")

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


class LocalDRMPlaylistServer:
    """Serve local OGG, MP3, and FLAC tracks to DecDRM without preloading them."""

    def __init__(
        self,
        files: Iterable[str],
        *,
        shuffle: bool = False,
        loop: bool = False,
        on_track: Callable[[str, int, int], None] | None = None,
    ) -> None:
        self.files = [
            Path(item).expanduser().resolve()
            for item in files
            if Path(item).suffix.casefold() in DRM_PLAYLIST_EXTENSIONS and Path(item).is_file()
        ]
        if not self.files:
            raise ValueError("DRM playlists need at least one OGG, MP3, or FLAC file.")

        self.shuffle = bool(shuffle)
        self.loop = bool(loop)
        self.on_track = on_track
        self.finished = threading.Event()
        self._stopped = threading.Event()
        self._lock = threading.Lock()
        self._rng = random.Random()
        self._previous: str | None = None
        self._order = build_playlist_order([str(path) for path in self.files], shuffle=self.shuffle, rng=self._rng)
        self._index = 0
        self._tokens: dict[str, Path] = {}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format, *_args) -> None:
                return

            def do_GET(self) -> None:
                request_path = urlsplit(self.path).path
                if request_path == "/playlist.m3u":
                    self._send_playlist()
                    return
                parts = request_path.split("/")
                if len(parts) >= 3 and parts[1] == "track":
                    self._send_track(parts[2])
                    return
                self.send_error(404)

            def _send_playlist(self) -> None:
                track = owner._next_track()
                if track is None:
                    body = b"Playlist finished."
                    self.send_response(410)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    try:
                        self.wfile.write(body)
                    except OSError:
                        pass
                    self.close_connection = True
                    return

                token, path = track
                url = f"http://127.0.0.1:{owner.port}/track/{token}/{quote(path.name)}"
                body = ("#EXTM3U\n" + url + "\n").encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "audio/x-mpegurl; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass
                self.close_connection = True

            def _send_track(self, token: str) -> None:
                path = owner._take_token(token)
                if path is None:
                    self.send_error(404)
                    return
                try:
                    size = path.stat().st_size
                    handle = path.open("rb")
                except OSError:
                    self.send_error(404)
                    return

                content_type = {
                    ".ogg": "audio/ogg",
                    ".mp3": "audio/mpeg",
                    ".flac": "audio/flac",
                }.get(path.suffix.casefold(), "application/octet-stream")
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(size))
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    with handle:
                        while not owner._stopped.is_set():
                            chunk = handle.read(64 * 1024)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    self.close_connection = True

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port = int(self._server.server_address[1])
        self.url = f"http://127.0.0.1:{self.port}/playlist.m3u"
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="txitpy-drm-playlist-http",
        )
        self._thread.start()

    def _next_track(self) -> tuple[str, Path] | None:
        with self._lock:
            if self._stopped.is_set() or self.finished.is_set():
                return None
            if self._index >= len(self._order):
                if not self.loop:
                    self.finished.set()
                    return None
                self._order = build_playlist_order(
                    [str(path) for path in self.files],
                    shuffle=self.shuffle,
                    previous_file=self._previous,
                    rng=self._rng,
                )
                self._index = 0
            track_index = self._index + 1
            path = Path(self._order[self._index])
            self._index += 1
            self._previous = str(path)
            token = secrets.token_urlsafe(18)
            self._tokens[token] = path

        if self.on_track:
            self.on_track(str(path), track_index, len(self.files))
        return token, path

    def _take_token(self, token: str) -> Path | None:
        with self._lock:
            return self._tokens.pop(token, None)

    def close(self) -> None:
        self._stopped.set()
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
