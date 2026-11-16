"""Windows system tray support for TXITPY."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def create_tray_image():
    """Create a small TXITPY icon using the application's primary blue."""
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (3, 3, 61, 61),
        radius=13,
        fill="#185ABC",
    )
    draw.rounded_rectangle(
        (14, 15, 50, 23),
        radius=3,
        fill="#FFFFFF",
    )
    draw.rounded_rectangle(
        (28, 21, 36, 49),
        radius=3,
        fill="#FFFFFF",
    )
    return image


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
            "Open TXITPY",
            lambda _icon, _item: on_restore(),
            default=True,
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            "Quit TXITPY",
            lambda _icon, _item: on_quit(),
        ),
    )
    return pystray.Icon(
        "TXITPY",
        create_tray_image(),
        title="TXITPY",
        menu=menu,
    )
