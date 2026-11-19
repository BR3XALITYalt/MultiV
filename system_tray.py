"""Windows system tray support for MultiV."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app_assets import asset_path


def create_tray_image():
    """Load the shared application logo and prepare it for the system tray."""
    from PIL import Image

    with Image.open(asset_path("logo.png")) as source:
        return source.convert("RGBA").resize((64, 64), Image.Resampling.LANCZOS)


def create_tray_icon(
    *,
    on_restore: Callable[[], Any],
    on_quit: Callable[[], Any],
):
    """Build the tray icon with a primary-click restore action and a Quit menu."""
    import pystray

    if not callable(on_restore) or not callable(on_quit):
        raise TypeError("Tray actions must be callable.")

    menu = pystray.Menu(
        pystray.MenuItem(
            "Open MultiV",
            lambda _icon, _item: on_restore(),
            default=True,
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            "Quit MultiV",
            lambda _icon, _item: on_quit(),
        ),
    )
    return pystray.Icon(
        "MultiV",
        create_tray_image(),
        title="MultiV",
        menu=menu,
    )
