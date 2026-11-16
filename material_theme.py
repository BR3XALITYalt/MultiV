"""TeleDrive-inspired Material 3 palette and native rectangular Tk widgets."""

from __future__ import annotations


MATERIAL_3_EXPRESSIVE = {
    "name": "txity-m3",
    "display_name": "TXITY Material 3",
    "light": {
        "primary": "#1B6EF3",
        "onPrimary": "#FFFFFF",
        "primaryContainer": "#D9E2FF",
        "onPrimaryContainer": "#001945",
        "secondary": "#575E71",
        "onSecondary": "#FFFFFF",
        "secondaryContainer": "#DBE2F9",
        "onSecondaryContainer": "#141B2C",
        "tertiary": "#00677E",
        "onTertiary": "#FFFFFF",
        "tertiaryContainer": "#B4EBFF",
        "onTertiaryContainer": "#001F28",
        "error": "#BA1A1A",
        "onError": "#FFFFFF",
        "errorContainer": "#FFDAD6",
        "onErrorContainer": "#410002",
        "background": "#F9F9FF",
        "onBackground": "#1A1B20",
        "surface": "#F9F9FF",
        "onSurface": "#1A1B20",
        "surfaceVariant": "#E1E2EC",
        "onSurfaceVariant": "#44464F",
        "outline": "#757780",
        "outlineVariant": "#C5C6D0",
        "inverseSurface": "#2F3036",
        "inverseOnSurface": "#F1F0F7",
        "inversePrimary": "#B0C6FF",
        "surfaceDim": "#D9D9E0",
        "surfaceBright": "#F9F9FF",
        "surfaceContainerLowest": "#FFFFFF",
        "surfaceContainerLow": "#F3F3FA",
        "surfaceContainer": "#EDEDF4",
        "surfaceContainerHigh": "#E8E7EE",
        "surfaceContainerHighest": "#E2E2E9",
    },
    "dark": {
        "primary": "#B0C6FF",
        "onPrimary": "#002D6E",
        "primaryContainer": "#00429B",
        "onPrimaryContainer": "#D9E2FF",
        "secondary": "#BFC6DC",
        "onSecondary": "#293042",
        "secondaryContainer": "#3F4759",
        "onSecondaryContainer": "#DBE2F9",
        "tertiary": "#5CD5F9",
        "onTertiary": "#003642",
        "tertiaryContainer": "#004E5F",
        "onTertiaryContainer": "#B4EBFF",
        "error": "#FFB4AB",
        "onError": "#690005",
        "errorContainer": "#93000A",
        "onErrorContainer": "#FFDAD6",
        "background": "#111318",
        "onBackground": "#E2E2E9",
        "surface": "#111318",
        "onSurface": "#E2E2E9",
        "surfaceVariant": "#44464F",
        "onSurfaceVariant": "#C5C6D0",
        "outline": "#8F9099",
        "outlineVariant": "#44464F",
        "inverseSurface": "#E2E2E9",
        "inverseOnSurface": "#2F3036",
        "inversePrimary": "#1B6EF3",
        "surfaceDim": "#111318",
        "surfaceBright": "#37393E",
        "surfaceContainerLowest": "#0C0E13",
        "surfaceContainerLow": "#1A1B20",
        "surfaceContainer": "#1E1F25",
        "surfaceContainerHigh": "#282A2F",
        "surfaceContainerHighest": "#33353A",
    },
}


def apply_tk_theme(root, mode: str = "light") -> dict[str, str]:
    """Apply the selected Material palette to Tk and return its color roles."""
    if mode not in {"light", "dark"}:
        raise ValueError("Theme mode must be 'light' or 'dark'.")

    scheme = MATERIAL_3_EXPRESSIVE[mode]
    if root is None:
        return dict(scheme)

    from tkinter import ttk

    root.configure(background=scheme["background"])
    style = ttk.Style(root)
    style.theme_use("clam")

    style.configure(
        ".",
        font=("Segoe UI", 10),
        background=scheme["background"],
        foreground=scheme["onSurface"],
    )
    style.configure("TFrame", background=scheme["background"])
    style.configure(
        "TLabel",
        background=scheme["background"],
        foreground=scheme["onSurface"],
    )
    style.configure(
        "TEntry",
        fieldbackground=scheme["surfaceContainerLowest"],
        foreground=scheme["onSurface"],
        insertcolor=scheme["onSurface"],
        bordercolor=scheme["outline"],
        lightcolor=scheme["outline"],
        darkcolor=scheme["outline"],
        padding=(12, 8),
    )
    style.map(
        "TEntry",
        fieldbackground=[("disabled", scheme["surfaceContainer"]), ("!disabled", scheme["surfaceContainerLowest"])],
        foreground=[("disabled", scheme["onSurfaceVariant"]), ("!disabled", scheme["onSurface"])],
    )
    style.configure(
        "Material.TEntry",
        fieldbackground=scheme["surfaceContainerLowest"],
        foreground=scheme["onSurface"],
        insertcolor=scheme["onSurface"],
        bordercolor=scheme["outline"],
        lightcolor=scheme["outline"],
        darkcolor=scheme["outline"],
        padding=(12, 8),
    )
    style.map(
        "Material.TEntry",
        fieldbackground=[("focus", scheme["surfaceContainerLowest"])],
        bordercolor=[("focus", scheme["primary"])],
    )
    style.configure(
        "Material.Treeview",
        background=scheme["surfaceContainerLowest"],
        fieldbackground=scheme["surfaceContainerLowest"],
        foreground=scheme["onSurface"],
        rowheight=34,
        borderwidth=0,
        relief="flat",
        font=("Segoe UI", 9),
    )
    style.map(
        "Material.Treeview",
        background=[("selected", scheme["primaryContainer"])],
        foreground=[("selected", scheme["onPrimaryContainer"])],
    )
    style.configure(
        "Material.Treeview.Heading",
        background=scheme["surfaceContainerHigh"],
        foreground=scheme["onSurfaceVariant"],
        font=("Segoe UI", 9, "bold"),
        padding=(10, 9),
        borderwidth=0,
        relief="flat",
    )
    style.map(
        "Material.Treeview.Heading",
        background=[("active", scheme["surfaceContainerHighest"])],
    )
    style.configure(
        "Vertical.TScrollbar",
        background=scheme["surfaceContainerHigh"],
        troughcolor=scheme["surfaceContainerLow"],
        bordercolor=scheme["surfaceContainerLow"],
        arrowcolor=scheme["onSurfaceVariant"],
    )
    style.configure(
        "Horizontal.TScrollbar",
        background=scheme["surfaceContainerHigh"],
        troughcolor=scheme["surfaceContainerLow"],
        bordercolor=scheme["surfaceContainerLow"],
        arrowcolor=scheme["onSurfaceVariant"],
    )
    return dict(scheme)



def native_widget_classes(tk):
    """Create native rectangular surfaces and buttons without custom drawing."""

    class SurfaceFrame(tk.Frame):
        """A rectangular surface with a thin outline and predictable padding."""

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

    class FlatButton(tk.Button):
        """A native rectangular button with clear hover and keyboard focus states."""

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
            self._normal_background = background
            self._normal_foreground = foreground
            self._hover_background = hover_background
            self._hover_foreground = hover_foreground or foreground
            self._focus_color = focus_color or foreground

            options = {
                "text": text,
                "command": command,
                "font": font,
                "bg": background,
                "fg": foreground,
                "activebackground": hover_background,
                "activeforeground": self._hover_foreground,
                "disabledforeground": foreground,
                "relief": "flat",
                "overrelief": "flat",
                "bd": 0,
                "borderwidth": 0,
                "highlightthickness": 1,
                "highlightbackground": outside or parent.cget("bg"),
                "highlightcolor": self._focus_color,
                "padx": padding_x,
                "pady": padding_y,
                "anchor": anchor,
                "cursor": "hand2",
                "takefocus": True,
            }
            if min_width > 0:
                options["width"] = min_width
            super().__init__(parent, **options)
            self.bind("<Enter>", self._on_enter, add="+")
            self.bind("<Leave>", self._on_leave, add="+")

        def _on_enter(self, _event=None) -> None:
            self.configure(
                bg=self._hover_background,
                fg=self._hover_foreground,
                activebackground=self._hover_background,
                activeforeground=self._hover_foreground,
            )

        def _on_leave(self, _event=None) -> None:
            self.configure(
                bg=self._normal_background,
                fg=self._normal_foreground,
                activebackground=self._hover_background,
                activeforeground=self._hover_foreground,
            )

    return SurfaceFrame, FlatButton
