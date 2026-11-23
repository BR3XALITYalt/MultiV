"""Centralized writable application data paths."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

_MIGRATION_COMPLETE = False


def _migrate_legacy_data(destination: Path) -> None:
    """Move existing Windows user data into the new installation data folder."""
    global _MIGRATION_COMPLETE
    if _MIGRATION_COMPLETE or os.name != "nt" or not getattr(sys, "frozen", False):
        return
    _MIGRATION_COMPLETE = True
    appdata = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    programdata = Path(os.environ.get("PROGRAMDATA") or Path(os.environ.get("SystemDrive", "C:")) / "ProgramData")
    copies = (
        (appdata / "TXITPY" / "transmit-settings.json", destination / "transmit-settings.json"),
        (programdata / "TXITPY" / "token.txt", destination / "token.txt"),
    )
    for source, target in copies:
        if source.is_file() and not target.exists():
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            except OSError:
                pass
    directories = (
        (local / "TXITPY" / "BrowserProfile", destination / "browser-profile"),
        (local / "TXITPY" / "logs", destination / "logs"),
        (local / "TXITPY" / "DRM", destination / "cache" / "TXITPY" / "DRM"),
        (local / "TXITPY" / "rade-v1", destination / "cache" / "TXITPY" / "rade-v1"),
    )
    for source, target in directories:
        if source.is_dir() and not target.exists():
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(source, target)
            except OSError:
                pass


def app_data_dir() -> Path:
    """Return the writable data directory beside the installed application."""
    override = os.environ.get("MULTIV_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parent
    destination = base / "data"
    _migrate_legacy_data(destination)
    return destination


def app_data_path(*parts: str) -> Path:
    return app_data_dir().joinpath(*parts)
