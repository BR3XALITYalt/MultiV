import unittest

from material_theme import MATERIAL_3_EXPRESSIVE, apply_tk_theme


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
    def test_uses_accessible_teledrive_derived_blue_palette(self):
        self.assertEqual(MATERIAL_3_EXPRESSIVE["light"]["primary"], "#185ABC")
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
        self.assertEqual(light["primary"], "#185ABC")
        self.assertEqual(dark["primary"], "#B0C6FF")

    def test_primary_text_and_outline_meet_contrast_requirements(self):
        surface_roles = (
            "background",
            "surface",
            "surfaceContainerLowest",
            "surfaceContainerLow",
            "surfaceContainer",
            "surfaceContainerHigh",
            "surfaceContainerHighest",
        )
        for mode in ("light", "dark"):
            scheme = MATERIAL_3_EXPRESSIVE[mode]
            for background in surface_roles:
                with self.subTest(mode=mode, background=background):
                    self.assertGreaterEqual(
                        _contrast(scheme["primary"], scheme[background]),
                        4.5,
                    )
            self.assertGreaterEqual(
                _contrast(scheme["outline"], scheme["surfaceContainerLow"]),
                3.0,
            )

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
