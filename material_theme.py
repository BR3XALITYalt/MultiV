"""Sun Valley ttk integration and matching colors for legacy Tk widgets."""

from __future__ import annotations

import sv_ttk


def _sun_valley_palette(mode: str) -> dict[str, str]:
    if mode == "dark":
        return {
            "primary": "#57C8FF",
            "onPrimary": "#001E2B",
            "primaryContainer": "#2F60D8",
            "onPrimaryContainer": "#FFFFFF",
            "secondary": "#C5C5C5",
            "onSecondary": "#1C1C1C",
            "secondaryContainer": "#333333",
            "onSecondaryContainer": "#F5F5F5",
            "tertiary": "#8BD5FF",
            "onTertiary": "#002B40",
            "tertiaryContainer": "#164B70",
            "onTertiaryContainer": "#D8F0FF",
            "error": "#FFB4AB",
            "onError": "#690005",
            "errorContainer": "#93000A",
            "onErrorContainer": "#FFDAD6",
            "background": "#1C1C1C",
            "onBackground": "#FAFAFA",
            "surface": "#1C1C1C",
            "onSurface": "#FAFAFA",
            "surfaceVariant": "#383838",
            "onSurfaceVariant": "#C6C6C6",
            "outline": "#777777",
            "outlineVariant": "#414141",
            "inverseSurface": "#FAFAFA",
            "inverseOnSurface": "#1C1C1C",
            "inversePrimary": "#005A80",
            "surfaceDim": "#171717",
            "surfaceBright": "#303030",
            "surfaceContainerLowest": "#202020",
            "surfaceContainerLow": "#242424",
            "surfaceContainer": "#282828",
            "surfaceContainerHigh": "#2D2D2D",
            "surfaceContainerHighest": "#383838",
        }

    return {
        "primary": "#005FB8",
        "onPrimary": "#FFFFFF",
        "primaryContainer": "#DCEAFE",
        "onPrimaryContainer": "#001D35",
        "secondary": "#5B5B5B",
        "onSecondary": "#FFFFFF",
        "secondaryContainer": "#E8E8E8",
        "onSecondaryContainer": "#202020",
        "tertiary": "#006A8E",
        "onTertiary": "#FFFFFF",
        "tertiaryContainer": "#C8E8F8",
        "onTertiaryContainer": "#001F2A",
        "error": "#BA1A1A",
        "onError": "#FFFFFF",
        "errorContainer": "#FFDAD6",
        "onErrorContainer": "#410002",
        "background": "#FAFAFA",
        "onBackground": "#1C1C1C",
        "surface": "#FAFAFA",
        "onSurface": "#1C1C1C",
        "surfaceVariant": "#E8E8E8",
        "onSurfaceVariant": "#505050",
        "outline": "#8A8A8A",
        "outlineVariant": "#D0D0D0",
        "inverseSurface": "#303030",
        "inverseOnSurface": "#F5F5F5",
        "inversePrimary": "#8CCBFF",
        "surfaceDim": "#E5E5E5",
        "surfaceBright": "#FAFAFA",
        "surfaceContainerLowest": "#FFFFFF",
        "surfaceContainerLow": "#F4F4F4",
        "surfaceContainer": "#EEEEEE",
        "surfaceContainerHigh": "#E8E8E8",
        "surfaceContainerHighest": "#DEDEDE",
    }


def apply_tk_theme(root, mode: str = "dark") -> dict[str, str]:
    """Apply the official Sun Valley ttk theme and return matching Tk colors."""
    if mode not in {"light", "dark"}:
        raise ValueError("Theme mode must be 'light' or 'dark'.")

    palette = _sun_valley_palette(mode)
    if root is not None:
        sv_ttk.set_theme(mode, root=root)
        root.configure(background=palette["background"])
    return palette


def native_widget_classes(tk):
    """Provide the legacy surface and button interfaces using Sun Valley ttk."""

    from tkinter import ttk

    class SurfaceFrame(tk.Frame):
        """A flat Tk surface that follows the selected Sun Valley color palette."""

        def __init__(
            self,
            parent,
            *,
            background: str,
            outside: str | None = None,
            outline: str | None = None,
            padding: int = 18,
            padding_x: int | None = None,
            padding_y: int | None = None,
            width: int | None = None,
        ) -> None:
            self._surface_background = background
            self._outside_background = outside or parent.cget("bg")
            self._outline_color = outline or background
            super().__init__(
                parent,
                bg=background,
                bd=0,
                relief="flat",
                highlightthickness=1 if outline else 0,
                highlightbackground=self._outline_color,
                highlightcolor=self._outline_color,
                padx=padding if padding_x is None else padding_x,
                pady=padding if padding_y is None else padding_y,
                width=width if width is not None else 0,
            )

    class FlatButton(ttk.Button):
        """Compatibility wrapper that renders with Sun Valley's native button."""

        def __init__(
            self,
            parent,
            text: str,
            command,
            *,
            background: str,
            foreground: str,
            hover_background: str,
            hover_foreground: str | None = None,
            outside: str | None = None,
            outline: str | None = None,
            focus_color: str | None = None,
            font=("Segoe UI", 10),
            padding_x: int = 16,
            padding_y: int = 9,
            anchor: str = "center",
            min_width: int = 0,
        ) -> None:
            options = {
                "text": text,
                "command": command,
                "style": "TButton",
                "cursor": "hand2",
                "padding": (padding_x, padding_y),
                "takefocus": True,
                "anchor": anchor,
            }
            if min_width > 0:
                options["width"] = min_width
            super().__init__(parent, **options)

    return SurfaceFrame, FlatButton
