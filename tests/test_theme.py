import tkinter as tk
import unittest
from tkinter import ttk

from material_theme import (
    _sun_valley_palette,
    apply_tk_theme,
    native_widget_classes,
)


REQUIRED_ROLES = {
    "primary", "onPrimary", "primaryContainer", "onPrimaryContainer",
    "secondary", "onSecondary", "secondaryContainer", "onSecondaryContainer",
    "tertiary", "onTertiary", "tertiaryContainer", "onTertiaryContainer",
    "error", "onError", "errorContainer", "onErrorContainer",
    "background", "onBackground", "surface", "onSurface", "surfaceVariant",
    "onSurfaceVariant", "outline", "outlineVariant", "inverseSurface",
    "inverseOnSurface", "inversePrimary", "surfaceDim", "surfaceBright",
    "surfaceContainerLowest", "surfaceContainerLow", "surfaceContainer",
    "surfaceContainerHigh", "surfaceContainerHighest",
}


class SunValleyThemeTests(unittest.TestCase):
    def test_palette_contains_all_legacy_tk_color_roles(self):
        for mode in ("light", "dark"):
            with self.subTest(mode=mode):
                self.assertTrue(REQUIRED_ROLES.issubset(_sun_valley_palette(mode)))

    def test_dark_palette_matches_sun_valley_dark_identity(self):
        palette = apply_tk_theme(None, "dark")
        self.assertEqual(palette["background"], "#1C1C1C")
        self.assertEqual(palette["onSurface"], "#FAFAFA")
        self.assertEqual(palette["primary"], "#57C8FF")

    def test_light_palette_matches_sun_valley_light_identity(self):
        palette = apply_tk_theme(None, "light")
        self.assertEqual(palette["background"], "#FAFAFA")
        self.assertEqual(palette["onSurface"], "#1C1C1C")
        self.assertEqual(palette["primary"], "#005FB8")

    def test_unknown_theme_is_rejected(self):
        with self.assertRaises(ValueError):
            apply_tk_theme(None, "automatic")

    def test_button_compatibility_class_uses_ttk(self):
        _surface_frame, button_class = native_widget_classes(tk)
        self.assertTrue(issubclass(button_class, ttk.Button))


if __name__ == "__main__":
    unittest.main()
