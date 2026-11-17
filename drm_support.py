"""DRM30 encoder setup and bounded local playlist input for TXITPY."""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
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
from urllib.request import Request, urlopen

from audio_playlist import build_playlist_order

DECDRM_REPOSITORY = "https://github.com/CasualArclamp/DecDRM.git"
DECDRM_COMMIT = "05845c9a754309b86d4985b25b1c3a787b988104"
DECDRM_VERSION = "0.7.1"
DRM_PLAYLIST_EXTENSIONS = frozenset({".flac", ".mp3", ".ogg"})
DRM_AUDIO_CODECS = ("xHE-AAC", "HE-AAC")
DRM_INTERLEAVING_OPTIONS = ("long", "short")
DRM_MSC_MODES = ("16-QAM", "64-QAM")

DRM_LANGUAGES = (
    "No language specified",
    "Arabic",
    "Bengali",
    "Chinese (Mandarin)",
    "Dutch",
    "English",
    "French",
    "German",
    "Hindi",
    "Japanese",
    "Javanese",
    "Korean",
    "Portuguese",
    "Russian",
    "Spanish",
    "Other language",
)

DRM_PROGRAMME_TYPES = (
    "No programme type",
    "News",
    "Current Affairs",
    "Information",
    "Sport",
    "Education",
    "Drama",
    "Culture",
    "Science",
    "Varied",
    "Pop Music",
    "Rock Music",
    "Easy Listening Music",
    "Light Classical",
    "Serious Classical",
    "Other Music",
    "Weather/meteorology",
    "Finance/Business",
    "Children's programmes",
    "Social Affairs",
    "Religion",
    "Phone In",
    "Travel",
    "Leisure",
    "Jazz Music",
    "Country Music",
    "National Music",
    "Oldies Music",
    "Folk Music",
    "Documentary",
    "Not used",
)


def _cache_root() -> Path:
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or Path.home())
        return root / "TXITPY" / "DRM"
    root = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    return root / "txitpy" / "drm"



RUST_MINIMUM_VERSION = (1, 88)
RUSTUP_DOWNLOAD_BASE = "https://static.rust-lang.org/rustup/dist"
VISUAL_STUDIO_BUILD_TOOLS_URL = "https://aka.ms/vs/stable/vs_buildtools.exe"
VISUAL_STUDIO_WORKLOAD = "Microsoft.VisualStudio.Workload.VCTools"
VISUAL_STUDIO_CPP_COMPONENT = "Microsoft.VisualStudio.Component.VC.Tools.x86.x64"
VISUAL_STUDIO_BUILD_TOOLS_LABEL = "Visual Studio C++ Build Tools (Desktop development with C++)"


def _rust_bin_directory() -> Path:
    cargo_home = Path(os.environ.get("CARGO_HOME") or (Path.home() / ".cargo"))
    return cargo_home / "bin"


def _refresh_rust_path() -> None:
    """Make an existing per-user rustup installation visible to this process."""
    rust_bin = _rust_bin_directory()
    if not rust_bin.is_dir():
        return
    current = os.environ.get("PATH", "")
    entries = current.split(os.pathsep) if current else []
    if str(rust_bin) not in entries:
        os.environ["PATH"] = str(rust_bin) + (os.pathsep + current if current else "")


def _rust_toolchain_version() -> tuple[int, int] | None:
    _refresh_rust_path()
    rustc = shutil.which("rustc")
    cargo = shutil.which("cargo")
    if not rustc or not cargo:
        return None
    try:
        result = subprocess.run(
            [rustc, "--version"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    match = re.search(r"rustc ([0-9]+)[.]([0-9]+)", result.stdout or result.stderr)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _encoder_executable() -> Path:
    return _cache_root() / f"DecDRM-{DECDRM_COMMIT[:12]}" / "target" / "release" / (
        "txitpy-drm-bridge.exe" if os.name == "nt" else "txitpy-drm-bridge"
    )


def _decdrm_source_is_ready() -> bool:
    upstream = _cache_root() / f"DecDRM-{DECDRM_COMMIT[:12]}"
    return (
        (upstream / ".git").is_dir()
        and (upstream / "crates" / "decdrm-station" / "src" / "plan.rs").is_file()
    )


def _vswhere_executable() -> Path | None:
    """Find the Visual Studio installer discovery utility."""
    roots = [
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("ProgramFiles"),
    ]
    for root in roots:
        if not root:
            continue
        candidate = Path(root) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
        if candidate.is_file():
            return candidate
    return None


def _visual_studio_installation_info(
    *,
    require_cpp: bool,
) -> tuple[Path, str] | None:
    """Return an installed Visual Studio path and its four-part version."""
    vswhere = _vswhere_executable()
    if vswhere is None:
        return None

    command = [
        str(vswhere),
        "-all" if require_cpp else "-latest",
        "-products",
        "*",
    ]
    if require_cpp:
        command.extend(["-requires", VISUAL_STUDIO_CPP_COMPONENT])
    command.extend(["-format", "json"])

    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None

    try:
        installations = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(installations, list):
        return None

    for installation in installations:
        if not isinstance(installation, dict):
            continue
        candidate_text = installation.get("installationPath")
        if not isinstance(candidate_text, str) or not candidate_text.strip():
            continue
        candidate = Path(candidate_text)
        if not candidate.is_dir():
            continue
        version = str(installation.get("installationVersion") or "")

        if require_cpp:
            vc_root = candidate / "VC"
            tool_root = vc_root / "Tools" / "MSVC"
            build_environment = vc_root / "Auxiliary" / "Build" / "vcvars64.bat"
            if not (
                build_environment.is_file()
                and tool_root.is_dir()
                and any(item.is_dir() for item in tool_root.iterdir())
            ):
                continue
        return candidate, version
    return None


def _visual_studio_installation_path(*, require_cpp: bool) -> Path | None:
    """Return the newest Visual Studio installation, optionally requiring MSVC."""
    installation = _visual_studio_installation_info(require_cpp=require_cpp)
    return installation[0] if installation else None


def _visual_studio_cmake_generator_for_version(version: str) -> str | None:
    """Map a Visual Studio installation version to its matching CMake generator."""
    match = re.match(r"^([0-9]+)", version)
    if not match:
        return None
    generators = {
        15: "Visual Studio 15 2017",
        16: "Visual Studio 16 2019",
        17: "Visual Studio 17 2022",
        18: "Visual Studio 18 2026",
    }
    return generators.get(int(match.group(1)))


def _visual_studio_cmake_generator() -> str | None:
    """Choose the CMake generator that matches the installed MSVC toolchain."""
    if os.name != "nt":
        return None
    installation = _visual_studio_installation_info(require_cpp=True)
    if installation is None:
        return None
    return _visual_studio_cmake_generator_for_version(installation[1])


def _visual_studio_cpp_tools_ready() -> bool:
    if os.name != "nt":
        return True
    return _visual_studio_installation_path(require_cpp=True) is not None


def _download_visual_studio_build_tools(
    on_log: Callable[[str], None] | None = None,
) -> Path:
    """Download the official Build Tools bootstrapper."""
    installer_dir = _cache_root() / "installers"
    installer_dir.mkdir(parents=True, exist_ok=True)
    installer = installer_dir / "vs_BuildTools.exe"

    if on_log:
        on_log("Downloading Microsoft's Visual Studio C++ Build Tools installer.")
    request = Request(
        VISUAL_STUDIO_BUILD_TOOLS_URL,
        headers={"User-Agent": "TXITPY/0.1"},
    )
    with urlopen(request, timeout=90) as response:
        data = response.read(128 * 1024 * 1024 + 1)
    if not data or len(data) > 128 * 1024 * 1024:
        raise RuntimeError("The Visual Studio Build Tools installer download was empty or too large.")

    temporary = installer.with_suffix(".download")
    temporary.write_bytes(data)
    temporary.replace(installer)

    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        raise RuntimeError("Windows PowerShell is required to verify the Microsoft installer signature.")
    signature_script = (
        "$signature = Get-AuthenticodeSignature -LiteralPath $args[0]; "
        "if ($signature.Status -ne 'Valid' -or "
        "$signature.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation') { exit 1 }"
    )
    try:
        result = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                signature_script,
                str(installer),
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        installer.unlink(missing_ok=True)
        raise RuntimeError(f"Could not verify the Microsoft installer signature: {exc}") from exc
    if result.returncode != 0:
        installer.unlink(missing_ok=True)
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(
            "The downloaded Visual Studio installer does not have a valid Microsoft signature."
            + (f"\n{detail}" if detail else "")
        )
    return installer


def _visual_studio_build_tools_command(
    installer: Path,
    existing_installation: Path | None = None,
) -> list[str]:
    """Build the installer command without modifying unrelated Visual Studio editions."""
    command = [str(installer)]
    if existing_installation is not None and existing_installation.name.casefold() == "buildtools":
        command.extend(["modify", "--installPath", str(existing_installation)])
    command.extend([
        "--wait",
        "--passive",
        "--norestart",
        "--add",
        VISUAL_STUDIO_WORKLOAD,
        "--includeRecommended",
    ])
    return command


def _install_visual_studio_cpp_tools(
    on_log: Callable[[str], None] | None = None,
) -> None:
    """Install the MSVC C++ workload required by the Windows DRM build."""
    if os.name != "nt" or _visual_studio_cpp_tools_ready():
        return

    installer = _download_visual_studio_build_tools(on_log)
    existing_installation = _visual_studio_installation_path(require_cpp=False)

    command = _visual_studio_build_tools_command(
        installer,
        existing_installation,
    )

    if on_log:
        on_log(
            "Installing the MSVC C++ compiler and Windows SDK. "
            "The Visual Studio Installer may request administrator approval."
        )
    try:
        process = subprocess.Popen(command)
        return_code = process.wait(timeout=4 * 60 * 60)
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(
            "The Visual Studio C++ Build Tools installer did not finish within four hours."
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"Could not start the Visual Studio Build Tools installer: {exc}") from exc

    if return_code != 0:
        raise RuntimeError(
            f"The Visual Studio C++ Build Tools installer exited with code {return_code}."
        )
    if not _visual_studio_cpp_tools_ready():
        raise RuntimeError(
            "The Visual Studio Installer finished, but TXITPY still cannot find the MSVC x64 C++ toolchain. "
            "Check that the Desktop development with C++ workload was installed."
        )
    if on_log:
        on_log("The MSVC C++ compiler and Windows SDK are ready.")


def _missing_native_build_tools() -> list[str]:
    missing = [
        name for name in ("git", "cmake")
        if shutil.which(name) is None
    ]
    if os.name == "nt":
        if not _visual_studio_cpp_tools_ready():
            missing.append(VISUAL_STUDIO_BUILD_TOOLS_LABEL)
    else:
        compiler_names = ("c++", "g++", "clang++")
        if not any(shutil.which(name) for name in compiler_names):
            missing.append("a C/C++ compiler")
    return missing


def drm_setup_status() -> dict[str, object]:
    """Report whether DRM is ready and which setup components are missing."""
    encoder = _encoder_executable()
    encoder_ready = encoder.is_file()
    rust_version = _rust_toolchain_version()
    rust_ready = rust_version is not None and rust_version >= RUST_MINIMUM_VERSION
    decdrm_ready = _decdrm_source_is_ready() or encoder_ready
    missing: list[str] = []
    if not encoder_ready:
        if not rust_ready:
            missing.append("Rust 1.88 or newer")
        if not decdrm_ready:
            missing.append(f"DecDRM {DECDRM_VERSION}")
        if not missing:
            missing.append("the TXITPY DRM encoder build")
    return {
        "ready": encoder_ready,
        "encoder_path": str(encoder) if encoder_ready else "",
        "rust_ready": rust_ready,
        "rust_version": rust_version,
        "decdrm_ready": decdrm_ready,
        "missing": missing,
        "missing_build_tools": _missing_native_build_tools() if not encoder_ready else [],
    }


def _rustup_windows_target() -> str:
    machine = platform.machine().casefold()
    if machine in {"arm64", "aarch64"}:
        return "aarch64-pc-windows-msvc"
    if platform.architecture()[0] == "32bit":
        return "i686-pc-windows-msvc"
    return "x86_64-pc-windows-msvc"


def _download_verified_rustup_installer(on_log: Callable[[str], None] | None = None) -> Path:
    if os.name != "nt":
        raise RuntimeError(
            "Automatic Rust installation is currently supported on Windows. "
            "Install Rust 1.88 or newer from https://rust-lang.org/tools/install, then reopen TXITPY."
        )
    target = _rustup_windows_target()
    url = f"{RUSTUP_DOWNLOAD_BASE}/{target}/rustup-init.exe"
    installer_dir = _cache_root() / "installers"
    installer_dir.mkdir(parents=True, exist_ok=True)
    installer = installer_dir / f"rustup-init-{target}.exe"

    if on_log:
        on_log("Downloading the official Rust installer.")
    request = Request(url, headers={"User-Agent": "TXITPY/0.1"})
    with urlopen(request, timeout=45) as response:
        installer_data = response.read(32 * 1024 * 1024 + 1)
    if not installer_data or len(installer_data) > 32 * 1024 * 1024:
        raise RuntimeError("The downloaded Rust installer was empty or unexpectedly large.")

    checksum_request = Request(url + ".sha256", headers={"User-Agent": "TXITPY/0.1"})
    with urlopen(checksum_request, timeout=30) as response:
        checksum_text = response.read(4_096).decode("ascii", errors="replace")
    expected_hash = checksum_text.strip().split()[0].casefold() if checksum_text.strip() else ""
    actual_hash = hashlib.sha256(installer_data).hexdigest()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_hash) or actual_hash != expected_hash:
        raise RuntimeError("The Rust installer checksum did not match. The file was not run.")

    temporary = installer.with_suffix(".download")
    temporary.write_bytes(installer_data)
    temporary.replace(installer)
    return installer


def _install_rust_toolchain(on_log: Callable[[str], None] | None = None) -> None:
    version = _rust_toolchain_version()
    if version is not None and version >= RUST_MINIMUM_VERSION:
        return

    installer = _download_verified_rustup_installer(on_log)
    if on_log:
        on_log("Installing the stable Rust toolchain. This can download several hundred megabytes.")
    command = [
        str(installer),
        "--default-toolchain", "stable",
        "--profile", "minimal",
        "-y",
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1_800,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("Rust installation did not finish within 30 minutes.") from exc
    except OSError as exc:
        raise RuntimeError(f"Could not start the Rust installer: {exc}") from exc

    _refresh_rust_path()
    version = _rust_toolchain_version()
    if result.returncode != 0 or version is None or version < RUST_MINIMUM_VERSION:
        output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
        detail = "\n".join(output.splitlines()[-20:])
        raise RuntimeError(
            "Rust installation did not produce a working Rust 1.88+ toolchain."
            + (f"\n{detail}" if detail else "")
        )
    if on_log:
        on_log(f"Rust {version[0]}.{version[1]} is ready.")


def prepare_drm_encoder(on_log: Callable[[str], None] | None = None) -> Path:
    """Install missing Rust, fetch the pinned DecDRM source, and build the encoder."""
    status = drm_setup_status()
    if status["ready"]:
        return Path(str(status["encoder_path"]))

    missing_tools = list(status["missing_build_tools"])
    if (
        os.name == "nt"
        and VISUAL_STUDIO_BUILD_TOOLS_LABEL in missing_tools
    ):
        _install_visual_studio_cpp_tools(on_log)
        status = drm_setup_status()
        missing_tools = list(status["missing_build_tools"])

    if missing_tools:
        raise RuntimeError(
            "DRM setup still needs these build tools: "
            + ", ".join(str(item) for item in missing_tools)
            + ". Install them, then select DRM again."
        )

    if not status["rust_ready"]:
        _install_rust_toolchain(on_log)

    if on_log and not status["decdrm_ready"]:
        on_log(f"Downloading DecDRM {DECDRM_VERSION} and its codec dependencies.")

    return ensure_drm_encoder(on_log=on_log)


def _run_command(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    on_log: Callable[[str], None] | None = None,
) -> str:
    try:
        process = subprocess.Popen(
            command,
            cwd=str(cwd) if cwd else None,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"Required build tool was not found: {command[0]}") from exc

    output_lines: list[str] = []
    assert process.stdout is not None
    for raw_line in process.stdout:
        line = raw_line.rstrip()
        if not line:
            continue
        output_lines.append(line)
        if len(output_lines) > 80:
            del output_lines[:-80]
        if on_log:
            on_log(line[:500])

    return_code = process.wait()
    output = "\n".join(output_lines).strip()
    if return_code != 0:
        tail = "\n".join(output.splitlines()[-50:])
        raise RuntimeError(
            f"Command failed ({return_code}): {' '.join(command)}"
            + (f"\n{tail}" if tail else "")
        )
    return output


def _patch_decdrm_output_validation(plan_file: Path) -> bool:
    """Allow TXITPY to consume returned samples without an extra output sink."""
    plan_source = plan_file.read_text(encoding="utf-8")
    newline = chr(10)
    output_check = (
        "    if out.file.is_none() && out.device.is_none() {" + newline
        + '        p.push("output: set ' + chr(96) + 'file' + chr(96) + ' and/or ' + chr(96) + 'device' + chr(96) + '");' + newline
        + "    }"
    )
    marker = "TXITPY consumes the samples returned by Station::transmit_frame()"
    if output_check in plan_source:
        plan_source = plan_source.replace(output_check, "    // " + marker + chr(10), 1)
        plan_file.write_text(plan_source, encoding="utf-8")
        return True
    if marker in plan_source:
        return False
    raise RuntimeError(
        "The pinned DecDRM source did not match the expected output validation code. "
        "TXITPY cannot safely prepare its in-memory output mode."
    )


def _clear_stale_opus_cmake_builds(
    build_root: Path,
    expected_generator: str,
    *,
    on_log: Callable[[str], None] | None = None,
) -> int:
    """Remove Opus CMake build directories configured with another generator."""
    removed = 0
    if not build_root.is_dir():
        return removed

    for cache_file in build_root.glob("decdrm-opus-sys-*/out/build/CMakeCache.txt"):
        try:
            cache_text = cache_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        configured_generator = next(
            (
                line.partition("=")[2].strip()
                for line in cache_text.splitlines()
                if line.startswith("CMAKE_GENERATOR:INTERNAL=")
            ),
            "",
        )
        if not configured_generator or configured_generator == expected_generator:
            continue

        build_directory = cache_file.parent
        shutil.rmtree(build_directory)
        removed += 1
        if on_log:
            on_log(
                "Cleared an old Opus build cache configured with "
                f"{configured_generator}; rebuilding with {expected_generator}."
            )

    return removed


def ensure_drm_encoder(on_log: Callable[[str], None] | None = None) -> Path:
    """Build the pinned DecDRM bridge once and return its executable path."""
    _refresh_rust_path()
    required_tools = ("git", "cargo", "rustc", "cmake")
    missing_tools = [tool for tool in required_tools if shutil.which(tool) is None]
    missing_tools.extend(_missing_native_build_tools())
    if missing_tools:
        raise RuntimeError(
            "DRM mode needs Git, Rust 1.88 or newer, CMake, and a C/C++ compiler. "
            "Missing: " + ", ".join(missing_tools) + "."
        )

    rust_version = _run_command(["rustc", "--version"]).strip()
    match = re.search("rustc ([0-9]+)[.]([0-9]+)", rust_version)
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
        build_environment = None
        if os.name == "nt":
            generator = _visual_studio_cmake_generator()
            if generator:
                build_environment = os.environ.copy()
                build_environment["CMAKE_GENERATOR"] = generator
                _clear_stale_opus_cmake_builds(
                    upstream / "target" / "release" / "build",
                    generator,
                    on_log=on_log,
                )
                if on_log:
                    on_log(f"Using {generator} to match the installed C++ build tools.")

        _run_command(
            ["cargo", "build", "--release", "-p", "txitpy-drm-bridge"],
            cwd=upstream,
            env=build_environment,
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
    tone_hz: float | None = None,
    tone_level_dbfs: float = -12.041,
    service_label: str | None = None,
    service_id: str = "0xB30001",
    language: str = "English",
    programme_type: str = "No programme type",
    iso_language: str = "",
    iso_country: str = "",
    audio_codec: str = "xHE-AAC",
    msc_mode: str = "16-QAM",
    interleaving: str = "long",
) -> Path:
    """Write a one-service, 4.5 kHz DRM30 station configuration."""
    input_count = sum((bool(input_device_name), bool(playlist_url), tone_hz is not None))
    if input_count != 1:
        raise ValueError("Choose exactly one DRM audio input: microphone, playlist, or test tone.")

    tone_frequency = None
    tone_level = None
    if tone_hz is not None:
        try:
            tone_frequency = float(tone_hz)
            tone_level = float(tone_level_dbfs)
        except (TypeError, ValueError) as exc:
            raise ValueError("The DRM test tone settings must be numeric.") from exc
        if not math.isfinite(tone_frequency) or not 0.0 < tone_frequency < 12_000.0:
            raise ValueError("The DRM test tone frequency must be between 0 and 12000 Hz.")
        if not math.isfinite(tone_level) or tone_level > 0.0:
            raise ValueError("The DRM test tone level must be a finite value at or below 0 dBFS.")

    raw_label = service_label if service_label is not None and str(service_label).strip() else station_name
    label = "".join(character for character in str(raw_label or "TXITPY") if character.isprintable())
    label = label.strip()[:16] or "TXITPY"

    service_id_text = str(service_id or "").strip()
    if service_id_text.lower().startswith("0x"):
        service_id_text = service_id_text[2:]
    if not re.fullmatch(r"[0-9a-fA-F]{1,6}", service_id_text):
        raise ValueError("DRM service ID must be 1 to 6 hexadecimal digits, optionally prefixed with 0x.")
    service_id_value = int(service_id_text, 16)

    selected_language = str(language or "").strip()
    if selected_language not in DRM_LANGUAGES:
        raise ValueError("Choose a valid DRM service language.")

    selected_programme_type = str(programme_type or "").strip()
    if selected_programme_type not in DRM_PROGRAMME_TYPES:
        raise ValueError("Choose a valid DRM programme type.")

    selected_iso_language = str(iso_language or "").strip().lower()
    if selected_iso_language and not re.fullmatch(r"[a-z]{3}", selected_iso_language):
        raise ValueError("ISO language must be a three-letter code, such as eng.")

    selected_iso_country = str(iso_country or "").strip().lower()
    if selected_iso_country and not re.fullmatch(r"[a-z]{2}", selected_iso_country):
        raise ValueError("Country code must be a two-letter ISO 3166 code, such as us.")

    codec_aliases = {
        "xhe-aac": "xhe-aac",
        "heaac": "he-aac",
        "he-aac": "he-aac",
    }
    selected_codec = codec_aliases.get(str(audio_codec or "").strip().casefold())
    if selected_codec is None:
        raise ValueError("Choose xHE-AAC or HE-AAC for the DRM audio codec.")

    selected_interleaving = str(interleaving or "").strip().casefold()
    if selected_interleaving not in DRM_INTERLEAVING_OPTIONS:
        raise ValueError("DRM interleaving must be long or short.")

    selected_msc_mode = str(msc_mode or "").strip().upper()
    if selected_msc_mode not in DRM_MSC_MODES:
        raise ValueError("DRM MSC modulation must be 16-QAM or 64-QAM.")

    lines = [
        "[channel]",
        'mode = "B"',
        "occupancy = 0",
        f'msc_mode = "{selected_msc_mode}"',
        'sdc_mode = "16-QAM"',
        f'interleaving = "{selected_interleaving}"',
        "protection_b = 1",
        "protection_a = 0",
        "",
        "[output]",
        'sample_format = "float32"',
        'format = "real"',
        "if_hz = 1500.0",
        "level_dbfs = -15.0",
        "band_limit = true",
        "",
        "[[service]]",
        f"label = {_toml_string(label)}",
        f"id = 0x{service_id_value:06X}",
        f"language = {_toml_string(selected_language)}",
        f"programme_type = {_toml_string(selected_programme_type)}",
    ]
    if selected_iso_language:
        lines.append(f"iso_language = {_toml_string(selected_iso_language)}")
    if selected_iso_country:
        lines.append(f"iso_country = {_toml_string(selected_iso_country)}")
    lines.extend([
        "",
        "[service.audio]",
        f'codec = "{selected_codec}"',
    ])
    if selected_codec == "he-aac":
        lines.append("core_rate = 12000")
    lines.extend([
        "stereo = false",
        'part = "B"',
        "share = 1.0",
        "",
        "[service.audio.input]",
    ])
    if playlist_url:
        lines.extend((f"url = {_toml_string(playlist_url)}", "stream_titles = false"))
    elif tone_frequency is not None and tone_level is not None:
        lines.extend((
            f"tone_hz = {tone_frequency:.6g}",
            f"level_dbfs = {tone_level:.6g}",
        ))
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
