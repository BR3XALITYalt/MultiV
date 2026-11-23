"""Centralized writable application data paths."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def app_data_dir() -> Path:
    """Return the writable data directory beside the installed application."""
    override = os.environ.get("MULTIV_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parent
    return base / "data"


def app_data_path(*parts: str) -> Path:
    return app_data_dir().joinpath(*parts)
