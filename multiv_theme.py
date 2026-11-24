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


def register_ttkbootstrap_themes(style) -> None:
    """Register separate light and dark themes with the exact MultiV color roles."""
    from ttkbootstrap import Colors, ThemeDefinition

    for mode in ("light", "dark"):
        palette = _multiv_palette(mode)
        colors = Colors(
            primary=palette["accent"],
            secondary=palette["mutedForeground"],
            success="#107c10",
            info=palette["accent"],
            warning="#ffb900",
            danger="#d13438",
            light="#fafafa",
            dark="#1c1c1c",
            bg=palette["background"],
            fg=palette["foreground"],
            selectbg=palette["selectionBackground"],
            selectfg=palette["selectionForeground"],
            border=palette["outline"],
            inputfg=palette["foreground"],
            inputbg=palette["background"],
            active=palette["accent"],
        )
        style.register_theme(
            ThemeDefinition(
                name=f"multiv-{mode}",
                colors=colors,
                mode=mode,
            )
        )


def apply_ttkbootstrap_theme(root, style, mode: str = "dark") -> dict[str, str]:
    """Apply a registered ttkbootstrap theme and return its matching Tk palette."""
    palette = _multiv_palette(mode)
    style.theme_use(f"multiv-{mode}")
    if root is not None:
        root.configure(background=palette["background"])
    return palette
