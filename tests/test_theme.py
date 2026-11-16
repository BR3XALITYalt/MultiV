import unittest

from material_theme import (
    MATERIAL_3_EXPRESSIVE,
    apply_tk_theme,
    render_rounded_rectangle,
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


def _linear_channel(value):
    value /= 255
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def _luminance(color):
    channels = [
        _linear_channel(int(color[index:index + 2], 16))
        for index in (1, 3, 5)
    ]
    return sum(
        weight * channel
        for weight, channel in zip((0.2126, 0.7152, 0.0722), channels)
    )


def _contrast(first, second):
    high, low = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


class MaterialThemeTests(unittest.TestCase):
    def test_rounded_rectangle_render_has_transparent_corners(self):
        image = render_rounded_rectangle(100, 60, 24, "#1B6EF3")
        self.assertEqual(image.mode, "RGBA")
        self.assertEqual(image.size, (100, 60))
        self.assertEqual(image.getpixel((0, 0))[3], 0)
        self.assertEqual(image.getpixel((50, 0))[3], 255)
        self.assertEqual(image.getpixel((99, 59))[3], 0)

    def test_rounded_rectangle_render_antialiases_edges(self):
        image = render_rounded_rectangle(100, 60, 24, "#1B6EF3")
        alpha_values = {
            image.getpixel((x, y))[3]
            for x in range(0, 30)
            for y in range(0, 30)
        }
        self.assertTrue(any(0 < alpha < 255 for alpha in alpha_values))

    def test_rounded_rectangle_render_clamps_large_radius(self):
        image = render_rounded_rectangle(20, 10, 50, "#FFFFFF")
        self.assertEqual(image.size, (20, 10))
        self.assertEqual(image.getpixel((10, 5))[3], 255)

    def test_uses_teledrive_blue_expressive_palette(self):
        self.assertEqual(MATERIAL_3_EXPRESSIVE["light"]["primary"], "#1B6EF3")
        self.assertEqual(MATERIAL_3_EXPRESSIVE["light"]["tertiary"], "#00677E")
        self.assertEqual(MATERIAL_3_EXPRESSIVE["dark"]["primary"], "#B0C6FF")
        self.assertEqual(MATERIAL_3_EXPRESSIVE["dark"]["tertiary"], "#5CD5F9")

    def test_light_and_dark_schemes_include_material_roles(self):
        for mode in ("light", "dark"):
            with self.subTest(mode=mode):
                self.assertTrue(REQUIRED_ROLES.issubset(MATERIAL_3_EXPRESSIVE[mode]))

    def test_tk_theme_returns_selected_palette_without_a_window(self):
        light = apply_tk_theme(None, "light")
        dark = apply_tk_theme(None, "dark")
        self.assertEqual(light["primary"], "#1B6EF3")
        self.assertEqual(dark["primary"], "#B0C6FF")

    def test_tk_theme_rejects_unknown_mode(self):
        with self.assertRaises(ValueError):
            apply_tk_theme(None, "automatic")

    def test_text_pairs_meet_wcag_aa_contrast(self):
        pairs = (
            ("primary", "onPrimary"),
            ("primaryContainer", "onPrimaryContainer"),
            ("secondary", "onSecondary"),
            ("secondaryContainer", "onSecondaryContainer"),
            ("tertiary", "onTertiary"),
            ("tertiaryContainer", "onTertiaryContainer"),
            ("error", "onError"),
            ("errorContainer", "onErrorContainer"),
            ("background", "onBackground"),
            ("surface", "onSurface"),
        )
        for mode in ("light", "dark"):
            scheme = MATERIAL_3_EXPRESSIVE[mode]
            for background, foreground in pairs:
                with self.subTest(mode=mode, pair=(background, foreground)):
                    self.assertGreaterEqual(
                        _contrast(scheme[background], scheme[foreground]),
                        4.5,
                    )


if __name__ == "__main__":
    unittest.main()
