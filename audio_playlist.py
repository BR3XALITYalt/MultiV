"""Playlist file discovery and ordering for TXITPY."""

from __future__ import annotations

import random
from pathlib import Path
from typing import Iterable


SUPPORTED_AUDIO_EXTENSIONS = frozenset({
    ".aif",
    ".aiff",
    ".flac",
    ".m4a",
    ".mp3",
    ".ogg",
    ".opus",
    ".wav",
})


def normalize_audio_sources(sources: Iterable[str | Path]) -> list[str]:
    """Keep existing file and folder sources in a stable, de-duplicated form."""
    normalized: list[str] = []
    seen: set[str] = set()

    for source in sources:
        try:
            raw = str(source).strip()
            if not raw:
                continue
            path = Path(raw).expanduser()
            if path.exists():
                if path.is_file() and path.suffix.casefold() not in SUPPORTED_AUDIO_EXTENSIONS:
                    continue
                if not path.is_file() and not path.is_dir():
                    continue
            # Keep missing source paths so temporarily disconnected folders
            # and files can be restored without importing them again.
            resolved = str(path.resolve(strict=False))
        except (OSError, TypeError, ValueError, RuntimeError):
            continue

        identity = resolved.casefold()
        if identity in seen:
            continue
        seen.add(identity)
        normalized.append(resolved)

    return normalized


def build_playlist_snapshot(
    sources: Iterable[str | Path],
    excluded_files: Iterable[str | Path] = (),
) -> tuple[list[str], tuple[tuple[str, int, int], ...]]:
    """Return the current playable paths and a signature for file changes."""
    excluded: set[str] = set()
    for item in excluded_files:
        try:
            excluded.add(str(Path(item).expanduser().resolve(strict=False)).casefold())
        except (OSError, TypeError, ValueError, RuntimeError):
            excluded.add(str(item).casefold())

    files: list[str] = []
    signature: list[tuple[str, int, int]] = []
    for file_path in collect_audio_files(sources):
        if file_path.casefold() in excluded:
            continue
        try:
            stat = Path(file_path).stat()
        except OSError:
            continue
        files.append(file_path)
        signature.append((file_path.casefold(), stat.st_size, stat.st_mtime_ns))

    return files, tuple(signature)


def collect_audio_files(sources: Iterable[str | Path]) -> list[str]:
    """Expand files and folders into a stable, de-duplicated audio file list."""
    collected: list[str] = []
    seen: set[str] = set()

    for source in sources:
        try:
            path = Path(source).expanduser()
            if path.is_file():
                candidates = [path]
            elif path.is_dir():
                candidates = sorted(
                    (item for item in path.rglob("*") if item.is_file()),
                    key=lambda item: str(item).casefold(),
                )
            else:
                continue
        except (OSError, TypeError, ValueError):
            continue

        for candidate in candidates:
            if candidate.suffix.casefold() not in SUPPORTED_AUDIO_EXTENSIONS:
                continue
            try:
                resolved = str(candidate.resolve())
            except OSError:
                resolved = str(candidate.absolute())
            identity = resolved.casefold()
            if identity in seen:
                continue
            seen.add(identity)
            collected.append(resolved)

    return collected


def build_playlist_order(
    files: Iterable[str],
    *,
    shuffle: bool = False,
    previous_file: str | None = None,
    rng: random.Random | None = None,
) -> list[str]:
    """Return one playlist pass, avoiding a shuffle-boundary repeat when possible."""
    order = list(files)
    if shuffle and len(order) > 1:
        (rng or random).shuffle(order)
        if previous_file and order[0] == previous_file:
            replacement = next(
                (index for index, item in enumerate(order[1:], start=1) if item != previous_file),
                None,
            )
            if replacement is not None:
                order[0], order[replacement] = order[replacement], order[0]
    return order
