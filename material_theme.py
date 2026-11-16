"""TeleDrive-inspired Material 3 Expressive palette and native ttk styling."""

from __future__ import annotations

import math


def rounded_rectangle_points(
    width: float,
    height: float,
    radius: float,
    *,
    segments: int = 14,
) -> list[float]:
    """Return an evenly sampled rounded-rectangle outline in clockwise order."""
    if width <= 0 or height <= 0:
        raise ValueError("Rounded rectangle dimensions must be positive.")
    if segments < 2:
        raise ValueError("A rounded rectangle needs at least two arc segments.")

    radius = max(0.0, min(float(radius), width / 2, height / 2))
    if radius == 0:
        return [0, 0, width, 0, width, height, 0, height]

    corners = (
        (radius, radius, math.pi, 1.5 * math.pi),
        (width - radius, radius, 1.5 * math.pi, 2 * math.pi),
        (width - radius, height - radius, 0, 0.5 * math.pi),
        (radius, height - radius, 0.5 * math.pi, math.pi),
    )
    points: list[float] = []
    for center_x, center_y, start, end in corners:
        for step in range(segments + 1):
            angle = start + (end - start) * step / segments
            points.extend((
                center_x + radius * math.cos(angle),
                center_y + radius * math.sin(angle),
            ))
    return points



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



def rounded_widget_classes(tk):
    """Create rounded Tk widgets while keeping tkinter imports out of module loading."""
    from tkinter import font as tkfont

    class RoundedSurface(tk.Frame):
        """A frame with a rounded surface painted behind its child widgets."""

        def __init__(
            self,
            parent,
            *,
            background: str,
            outside: str | None = None,
            outline: str | None = None,
            padding: int = 18,
            radius: int = 18,
            width: int | None = None,
        ) -> None:
            self._surface_background = background
            self._outside_background = outside or parent.cget("bg")
            self._outline_color = outline or background
            self._corner_radius = radius
            inset = max(padding, math.ceil(radius * 0.34))
            super().__init__(
                parent,
                bg=self._outside_background,
                bd=0,
                highlightthickness=0,
                padx=inset,
                pady=inset,
                width=width if width is not None else 0,
            )
            self._surface_canvas = tk.Canvas(
                self,
                bg=self._outside_background,
                bd=0,
                highlightthickness=0,
                takefocus=0,
            )
            self._surface_canvas.place(x=0, y=0, relwidth=1, relheight=1)
            # Canvas.lower() lowers canvas items; use Tk's window command to stack this widget.
            self.tk.call("lower", self._surface_canvas._w)
            self.bind("<Configure>", self._redraw_surface, add="+")

        def _redraw_surface(self, event=None) -> None:
            width = self.winfo_width()
            height = self.winfo_height()

            # Tk sends an initial configure event before geometry is assigned.
            # Wait until the surface has room for its 1-pixel outline.
            if width <= 2 or height <= 2:
                return

            self._surface_canvas.delete("surface")
            coordinates = rounded_rectangle_points(
                width - 2,
                height - 2,
                min(self._corner_radius, (min(width, height) - 2) / 2),
            )
            coordinates = [value + 1 for value in coordinates]
            self._surface_canvas.create_polygon(
                *coordinates,
                fill=self._surface_background,
                outline=self._outline_color,
                width=1,
                tags="surface",
            )

    class RoundedButton(tk.Canvas):
        """A keyboard-accessible button with a rounded background."""

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
            radius: int = 14,
            anchor: str = "center",
            min_width: int = 0,
        ) -> None:
            self._text = text
            self._command = command
            self._background = background
            self._foreground = foreground
            self._hover_background = hover_background
            self._hover_foreground = hover_foreground or foreground
            self._outside = outside or parent.cget("bg")
            self._outline = outline or background
            self._focus_color = focus_color or foreground
            self._corner_radius = radius
            self._padding_x = padding_x
            self._anchor = anchor
            self._hovered = False
            self._focused = False
            self._font = tkfont.Font(root=parent, font=font)
            self._requested_width = max(
                min_width,
                self._font.measure(text) + padding_x * 2,
            )
            self._requested_height = max(
                34,
                self._font.metrics("linespace") + padding_y * 2,
            )
            super().__init__(
                parent,
                width=self._requested_width,
                height=self._requested_height,
                bg=self._outside,
                bd=0,
                highlightthickness=0,
                cursor="hand2",
                takefocus=True,
            )
            self.bind("<Configure>", self._redraw, add="+")
            self.bind("<Enter>", self._on_enter, add="+")
            self.bind("<Leave>", self._on_leave, add="+")
            self.bind("<Button-1>", self._on_click, add="+")
            self.bind("<space>", self._on_key, add="+")
            self.bind("<Return>", self._on_key, add="+")
            self.bind("<FocusIn>", self._on_focus, add="+")
            self.bind("<FocusOut>", self._on_blur, add="+")
            self._redraw()

        def _redraw(self, event=None) -> None:
            width = max(2, self.winfo_width())
            height = max(2, self.winfo_height())
            if width <= 2:
                width = self._requested_width
            if height <= 2:
                height = self._requested_height
            fill = self._hover_background if self._hovered else self._background
            foreground = self._hover_foreground if self._hovered else self._foreground
            outline = self._focus_color if self._focused else self._outline
            self.delete("all")
            coordinates = rounded_rectangle_points(
                width - 2,
                height - 2,
                min(self._corner_radius, (min(width, height) - 2) / 2),
            )
            coordinates = [value + 1 for value in coordinates]
            self.create_polygon(
                *coordinates,
                fill=fill,
                outline=outline,
                width=2 if self._focused else 1,
            )
            x = self._padding_x if self._anchor == "w" else width / 2
            self.create_text(
                x,
                height / 2,
                text=self._text,
                fill=foreground,
                font=self._font,
                anchor=self._anchor,
            )

        def _on_enter(self, _event=None) -> None:
            self._hovered = True
            self._redraw()

        def _on_leave(self, _event=None) -> None:
            self._hovered = False
            self._redraw()

        def _on_focus(self, _event=None) -> None:
            self._focused = True
            self._redraw()

        def _on_blur(self, _event=None) -> None:
            self._focused = False
            self._redraw()

        def _on_click(self, _event=None) -> str:
            self.focus_set()
            self._invoke()
            return "break"

        def _on_key(self, _event=None) -> str:
            self._invoke()
            return "break"

        def _invoke(self) -> None:
            if callable(self._command):
                self._command()

    return RoundedSurface, RoundedButton
