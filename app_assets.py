"""Resolve application assets in source and PyInstaller builds."""

from __future__ import annotations

import sys
from pathlib import Path


def asset_path(*parts: str) -> Path:
    """Return an asset path, preferring the active PyInstaller bundle when frozen."""
    if not parts:
        raise ValueError("At least one asset path component is required.")

    relative = Path(*parts)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("Asset paths must be relative and cannot traverse directories.")

    roots: list[Path] = []
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        roots.append(Path(bundle_root))

    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).resolve().parent)

    roots.append(Path(__file__).resolve().parent)

    for root in roots:
        candidate = root / "assets" / relative
        if candidate.is_file():
            return candidate

    return roots[0] / "assets" / relative
