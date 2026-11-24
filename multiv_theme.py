"""MultiV ttkbootstrap theme setup using the existing Sun Valley palette."""

from __future__ import annotations


def _multiv_palette(mode: str) -> dict[str, str]:
    """Return MultiV's light or dark palette, retaining the previous Sun Valley colors."""
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


def register_ttkbootstrap_themes():
    """Register the MultiV light/dark theme family using ttkbootstrap's 2.x API."""
    import ttkbootstrap as ttk

    theme = ttk.Theme(
        name="multiv",
        primary="#57c8ff",
        secondary="#595959",
        success="#107c10",
        info="#57c8ff",
        warning="#ffb900",
        danger="#d13438",
        neutral="#595959",
        light={
            "background": "#fafafa",
            "foreground": "#1c1c1c",
        },
        dark={
            "background": "#1c1c1c",
            "foreground": "#fafafa",
        },
    )
    return theme.register()


def apply_ttkbootstrap_theme(root, style, mode: str = "dark") -> dict[str, str]:
    """Apply a registered ttkbootstrap theme and return its matching Tk palette."""
    palette = _multiv_palette(mode)
    style.theme_use(f"multiv-{mode}")
    if root is not None:
        root.configure(background=palette["background"])
    return palette
