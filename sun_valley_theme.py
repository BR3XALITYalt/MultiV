"""Sun Valley ttk theme setup and matching colors for legacy Tk widgets."""

from __future__ import annotations

import sv_ttk


def _sun_valley_palette(mode: str) -> dict[str, str]:
    """Return the colors defined by the official Sun Valley theme."""
    if mode == "dark":
        return {
            "background": "#1c1c1c",
            "surface": "#1c1c1c",
            "surfaceContainerLow": "#1c1c1c",
            "surfaceContainerLowest": "#1c1c1c",
            "surfaceContainerHigh": "#1c1c1c",
            "onSurface": "#fafafa",
            "onSurfaceVariant": "#c5c5c5",
            "primary": "#57c8ff",
            "primaryContainer": "#2f60d8",
            "onPrimary": "#ffffff",
            "onPrimaryContainer": "#ffffff",
            "outline": "#595959",
            "outlineVariant": "#383838",
        }
    if mode == "light":
        return {
            "background": "#fafafa",
            "surface": "#fafafa",
            "surfaceContainerLow": "#fafafa",
            "surfaceContainerLowest": "#fafafa",
            "surfaceContainerHigh": "#fafafa",
            "onSurface": "#1c1c1c",
            "onSurfaceVariant": "#5b5b5b",
            "primary": "#005fb8",
            "primaryContainer": "#2f60d8",
            "onPrimary": "#ffffff",
            "onPrimaryContainer": "#ffffff",
            "outline": "#a0a0a0",
            "outlineVariant": "#d0d0d0",
        }
    raise ValueError("Theme mode must be 'light' or 'dark'.")


def apply_tk_theme(root, mode: str = "dark") -> dict[str, str]:
    """Apply Sun Valley to ttk widgets and return its native Tk color scheme."""
    palette = _sun_valley_palette(mode)
    if root is not None:
        sv_ttk.set_theme(mode, root=root)
        root.configure(background=palette["background"])
    return palette
