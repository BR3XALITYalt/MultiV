import unittest

from material_theme import MATERIAL_3_EXPRESSIVE


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
    def test_uses_teledrive_blue_expressive_palette(self):
        self.assertEqual(MATERIAL_3_EXPRESSIVE["light"]["primary"], "#1B6EF3")
        self.assertEqual(MATERIAL_3_EXPRESSIVE["light"]["tertiary"], "#00677E")
        self.assertEqual(MATERIAL_3_EXPRESSIVE["dark"]["primary"], "#B0C6FF")
        self.assertEqual(MATERIAL_3_EXPRESSIVE["dark"]["tertiary"], "#5CD5F9")

    def test_light_and_dark_schemes_include_material_roles(self):
        for mode in ("light", "dark"):
            with self.subTest(mode=mode):
                self.assertTrue(REQUIRED_ROLES.issubset(MATERIAL_3_EXPRESSIVE[mode]))

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
