"""Verified rolling-release updates for packaged MultiV builds."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shlex
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from build_info import APP_BUILD_NUMBER, APP_COMMIT, APP_VERSION

GITHUB_REPOSITORY = "BR3XALITYalt/MultiV"
ROLLING_TAG = "rolling"
RELEASE_API_URL = (
    f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/tags/{ROLLING_TAG}"
)
MAX_DOWNLOAD_BYTES = 1_500 * 1024 * 1024


@dataclass(frozen=True)
class UpdateInfo:
    """A release asset whose metadata and SHA-256 have been checked."""

    version: str
    build_number: int
    commit: str
    filename: str
    download_url: str
    sha256: str
    release_url: str


def is_packaged() -> bool:
    """Return True when running from a PyInstaller application bundle."""
    return bool(getattr(sys, "frozen", False))


def platform_key() -> str | None:
    """Return the release asset key for this operating system and CPU."""
    machine = platform.machine().casefold()
    if machine not in {"x86_64", "amd64"}:
        return None
    if sys.platform == "win32":
        return "windows-x86_64"
    if sys.platform.startswith("linux"):
        return "linux-x86_64"
    return None


def _request_bytes(url: str, *, timeout: float = 12.0) -> bytes:
    request = Request(
        url,
        headers={
            "User-Agent": "MultiV-Updater",
            "Accept": "application/vnd.github+json",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        return response.read(MAX_DOWNLOAD_BYTES + 1)


def _request_json(url: str) -> dict[str, Any]:
    data = _request_bytes(url)
    if not data or len(data) > MAX_DOWNLOAD_BYTES:
        raise RuntimeError("The update server returned an empty or oversized response.")
    try:
        result = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("The update server returned invalid JSON.") from exc
    if not isinstance(result, dict):
        raise RuntimeError("The update server returned an invalid response.")
    return result


def _expected_filename(key: str, build_number: int) -> str:
    return {
        "windows-x86_64": f"MultiV-windows-x86_64-{build_number}.exe",
        "linux-x86_64": f"MultiV-linux-x86_64-{build_number}.AppImage",
    }[key]


def parse_update_manifest(
    manifest: dict[str, Any],
    *,
    key: str,
    current_build: int = APP_BUILD_NUMBER,
    release_url: str = "",
) -> UpdateInfo | None:
    """Validate a rolling manifest and return a newer asset for this platform."""
    if manifest.get("channel") != "rolling":
        raise RuntimeError("The update manifest is not a rolling release.")
    try:
        build_number = int(manifest["build_number"])
        version = str(manifest["version"]).strip()
        commit = str(manifest["commit"]).strip()
        asset = manifest["assets"][key]
        filename = str(asset["filename"])
        download_url = str(asset["url"])
        digest = str(asset["sha256"]).casefold()
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("The update manifest is missing required fields.") from exc

    if build_number < 1 or not version or not commit:
        raise RuntimeError("The update manifest contains invalid build metadata.")
    if build_number <= int(current_build):
        return None
    if filename != _expected_filename(key, build_number):
        raise RuntimeError("The update manifest names an unexpected application package.")
    expected_url = (
        f"https://github.com/{GITHUB_REPOSITORY}/releases/download/"
        f"{ROLLING_TAG}/{filename}"
    )
    if download_url != expected_url:
        raise RuntimeError("The update manifest contains an unexpected download URL.")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise RuntimeError("The update manifest contains an invalid SHA-256 checksum.")

    return UpdateInfo(
        version=version,
        build_number=build_number,
        commit=commit,
        filename=filename,
        download_url=download_url,
        sha256=digest,
        release_url=release_url,
    )


def check_for_update() -> UpdateInfo | None:
    """Check the rolling release for a newer package matching this platform."""
    if not is_packaged():
        return None
    key = platform_key()
    if key is None:
        return None

    release = _request_json(RELEASE_API_URL)
    if release.get("draft") or not release.get("prerelease"):
        raise RuntimeError("The rolling update release is not published as a prerelease.")

    assets = release.get("assets")
    if not isinstance(assets, list):
        raise RuntimeError("The rolling release has no application assets.")
    manifest_asset = next(
        (
            asset for asset in assets
            if isinstance(asset, dict) and asset.get("name") == "multiv-update.json"
        ),
        None,
    )
    if manifest_asset is None:
        raise RuntimeError("The rolling release does not contain its update manifest.")

    manifest_url = str(manifest_asset.get("browser_download_url") or "")
    expected_manifest_url = (
        f"https://github.com/{GITHUB_REPOSITORY}/releases/download/"
        f"{ROLLING_TAG}/multiv-update.json"
    )
    if manifest_url != expected_manifest_url:
        raise RuntimeError("The rolling release contains an unexpected manifest URL.")

    manifest = _request_json(manifest_url)
    return parse_update_manifest(
        manifest,
        key=key,
        release_url=str(release.get("html_url") or ""),
    )


def _download_verified_update(info: UpdateInfo, destination: Path) -> Path:
    request = Request(
        info.download_url,
        headers={"User-Agent": "MultiV-Updater"},
    )
    digest = hashlib.sha256()
    total = 0
    try:
        with urlopen(request, timeout=60) as response, destination.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise RuntimeError("The update package exceeds the download size limit.")
                digest.update(chunk)
                output.write(chunk)
        if total == 0:
            raise RuntimeError("The update package was empty.")
        if digest.hexdigest() != info.sha256:
            raise RuntimeError("The update package SHA-256 checksum did not match.")
        return destination
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def _windows_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _schedule_windows_update(installer: Path) -> None:
    executable = Path(sys.executable).resolve()
    install_dir = executable.parent
    script_path = Path(tempfile.gettempdir()) / f"multiv-update-{uuid.uuid4().hex}.ps1"
    default_install_dir = Path(
        os.environ.get("ProgramFiles", r"C:\Program Files")
    ) / "MultiV"
    script = f"""$ErrorActionPreference = 'Stop'
$ParentProcessId = {os.getpid()}
$Installer = {_windows_quote(str(installer))}
$InstallDir = {_windows_quote(str(install_dir))}
$DefaultInstallDir = {_windows_quote(str(default_install_dir))}
$UninstallKey = 'HKLM:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{{7C8D40C3-9EAA-49E0-8CB1-7819402B4761}}_is1'
while (Get-Process -Id $ParentProcessId -ErrorAction SilentlyContinue) {{
    Start-Sleep -Milliseconds 300
}}
try {{
    # ZIP-based portable installs have no Inno Setup registration. Move those
    # users to the standard Program Files location during their first installer update.
    if (-not (Test-Path -LiteralPath $UninstallKey)) {{
        $InstallDir = $DefaultInstallDir
    }}
    $InstallerProcess = Start-Process -FilePath $Installer -ArgumentList @('/SILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-') -Verb RunAs -Wait -PassThru
    if ($InstallerProcess.ExitCode -ne 0) {{
        throw "The MultiV installer exited with code $($InstallerProcess.ExitCode)."
    }}
    $InstalledExecutable = Join-Path $InstallDir 'MultiV.exe'
    if (-not (Test-Path -LiteralPath $InstalledExecutable)) {{
        throw "The installer completed, but MultiV.exe was not found in $InstallDir."
    }}
    Start-Process -FilePath $InstalledExecutable -WorkingDirectory $InstallDir
}} catch {{
    $_ | Out-File -FilePath (Join-Path $env:TEMP 'MultiV-update-error.log') -Append
}} finally {{
    Remove-Item -LiteralPath $Installer -Force -ErrorAction SilentlyContinue
}}
"""
    script_path.write_text(script, encoding="utf-8")
    flags = (
        getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )
    try:
        subprocess.Popen(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            close_fds=True,
        )
    except Exception:
        script_path.unlink(missing_ok=True)
        installer.unlink(missing_ok=True)
        raise


def _schedule_linux_appimage_update(download: Path) -> None:
    appimage = os.environ.get("APPIMAGE")
    if not appimage:
        download.unlink(missing_ok=True)
        raise RuntimeError(
            "MultiV could not locate the running AppImage. Start MultiV from its .AppImage file to update it."
        )
    target = Path(appimage).expanduser().resolve()
    if not target.is_file() or not os.access(target.parent, os.W_OK):
        download.unlink(missing_ok=True)
        raise RuntimeError(
            "The AppImage is not in a writable folder. Move it to a folder your account can modify, then retry."
        )

    log_dir = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    log_dir = log_dir / "TXITPY"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "update.log"

    script_path = Path(tempfile.gettempdir()) / f"multiv-update-{uuid.uuid4().hex}.sh"
    script = "\n".join([
        "#!/bin/sh",
        "set -eu",
        f"LOG={shlex.quote(str(log_path))}",
        'exec >>"$LOG" 2>&1',
        f"PARENT_PID={os.getpid()}",
        f"DOWNLOAD={shlex.quote(str(download))}",
        f"TARGET={shlex.quote(str(target))}",
        'while kill -0 "$PARENT_PID" 2>/dev/null; do sleep 0.25; done',
        'chmod 755 "$DOWNLOAD"',
        'mv -f -- "$DOWNLOAD" "$TARGET"',
        '"$TARGET" >/dev/null 2>&1 &',
        'rm -f -- "$0"',
        "",
    ])
    script_path.write_text(script, encoding="utf-8")
    script_path.chmod(0o700)
    try:
        subprocess.Popen(
            ["/bin/sh", str(script_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except Exception:
        script_path.unlink(missing_ok=True)
        download.unlink(missing_ok=True)
        raise


def install_update(info: UpdateInfo) -> None:
    """Download and verify an update, then schedule replacement after this process exits."""
    if not is_packaged():
        raise RuntimeError("Updates are available only in the packaged application.")
    key = platform_key()
    if key is None:
        raise RuntimeError("Automatic updates are not available for this platform.")

    if key == "windows-x86_64":
        destination = Path(tempfile.gettempdir()) / f"multiv-update-{uuid.uuid4().hex}.exe"
        _download_verified_update(info, destination)
        _schedule_windows_update(destination)
        return

    if key == "linux-x86_64":
        appimage = os.environ.get("APPIMAGE")
        if not appimage:
            raise RuntimeError("MultiV must be started from its AppImage file to install updates.")
        target = Path(appimage).expanduser().resolve()
        if not target.is_file() or not os.access(target.parent, os.W_OK):
            raise RuntimeError(
                "The AppImage is not in a writable folder. Move it to a folder your account can modify, then retry."
            )
        with tempfile.NamedTemporaryFile(
            prefix=".multiv-update-",
            suffix=".AppImage",
            dir=target.parent,
            delete=False,
        ) as temporary:
            destination = Path(temporary.name)
        _download_verified_update(info, destination)
        _schedule_linux_appimage_update(destination)
        return

    raise RuntimeError("Automatic updates are not available for this platform.")
