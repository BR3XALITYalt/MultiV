"""TeleDrive-inspired Material 3 Expressive color scheme for TXITPY.

The palette follows TeleDrive's blue light/dark color schemes. Bootstack has
its own Tk widgets, so the Material roles are mapped onto Bootstack's semantic
accent and layered surface tokens instead of replacing its widget toolkit.
"""

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


def install_theme() -> None:
    """Register the light and dark TXITY Material-inspired theme variants."""
    from bootstack.style import Theme

    light = MATERIAL_3_EXPRESSIVE["light"]
    dark = MATERIAL_3_EXPRESSIVE["dark"]

    Theme.from_existing(
        "bootstrap",
        name=MATERIAL_3_EXPRESSIVE["name"],
        display_name=MATERIAL_3_EXPRESSIVE["display_name"],
        primary=light["primary"],
        secondary=light["secondary"],
        info=light["tertiary"],
        success="#2E7D32",
        warning="#8D5E00",
        danger=light["error"],
        neutral=light["outline"],
        light={
            "background": light["background"],
            "foreground": light["onSurface"],
        },
        dark={
            "background": dark["background"],
            "foreground": dark["onSurface"],
        },
        surfaces={
            "light": {
                "content": light["surface"],
                "card": light["surfaceContainerLow"],
                "chrome": light["background"],
                "raised": light["surfaceContainer"],
                "overlay": light["surfaceContainerHigh"],
                "input": light["surfaceContainerLowest"],
            },
            "dark": {
                "content": dark["surface"],
                "card": dark["surfaceContainerLow"],
                "chrome": dark["background"],
                "raised": dark["surfaceContainer"],
                "overlay": dark["surfaceContainerHigh"],
                "input": dark["surfaceContainerLowest"],
            },
        },
    ).install()
