"""Sun Valley ttk theme setup and matching colors for native Tk controls."""

from __future__ import annotations

import sv_ttk


def _sun_valley_palette(mode: str) -> dict[str, str]:
    """Return the native colors used by the selected Sun Valley theme."""
    if mode == "dark":
        return {
            "background": "#1c1c1c",
            "foreground": "#fafafa",
            "mutedForeground": "#595959",
            "accent": "#57c8ff",
            "selectionBackground": "#2f60d8",
            "selectionForeground": "#ffffff",
            "outline": "#595959",
            "outlineVariant": "#595959",
        }
    if mode == "light":
        return {
            "background": "#fafafa",
            "foreground": "#1c1c1c",
            "mutedForeground": "#a0a0a0",
            "accent": "#005fb8",
            "selectionBackground": "#2f60d8",
            "selectionForeground": "#ffffff",
            "outline": "#a0a0a0",
            "outlineVariant": "#a0a0a0",
        }
    raise ValueError("Theme mode must be 'light' or 'dark'.")


def apply_tk_theme(root, mode: str = "dark") -> dict[str, str]:
    """Apply the official Sun Valley ttk theme and return its native Tk colors."""
    palette = _sun_valley_palette(mode)
    if root is not None:
        sv_ttk.set_theme(mode, root=root)
        root.configure(background=palette["background"])
    return palette
