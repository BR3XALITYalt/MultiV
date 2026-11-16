"""Playlist file discovery and ordering for TXITPY."""

from __future__ import annotations

import random
from pathlib import Path
from typing import Iterable


SUPPORTED_AUDIO_EXTENSIONS = frozenset({
    ".aif",
    ".aiff",
    ".flac",
    ".mp3",
    ".ogg",
    ".wav",
})


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
