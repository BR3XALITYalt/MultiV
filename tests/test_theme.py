import unittest

from material_theme import BLUE_TONAL_SPOT


REQUIRED_COLOR_ROLES = {
    "primary", "onPrimary", "primaryContainer", "onPrimaryContainer",
    "inversePrimary", "primaryFixed", "primaryFixedDim", "onPrimaryFixed",
    "onPrimaryFixedVariant",
    "secondary", "onSecondary", "secondaryContainer", "onSecondaryContainer",
    "secondaryFixed", "secondaryFixedDim", "onSecondaryFixed",
    "onSecondaryFixedVariant",
    "tertiary", "onTertiary", "tertiaryContainer", "onTertiaryContainer",
    "tertiaryFixed", "tertiaryFixedDim", "onTertiaryFixed",
    "onTertiaryFixedVariant",
    "error", "onError", "errorContainer", "onErrorContainer",
    "background", "onBackground", "surface", "onSurface", "surfaceVariant",
    "onSurfaceVariant", "surfaceTint", "inverseSurface", "inverseOnSurface",
    "outline", "outlineVariant", "shadow", "scrim", "surfaceDim",
    "surfaceBright", "surfaceContainerLowest", "surfaceContainerLow",
    "surfaceContainer", "surfaceContainerHigh", "surfaceContainerHighest",
}


def _linear_channel(value):
    value /= 255
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def _luminance(color):
    channels = [
        _linear_channel(int(color[index:index + 2], 16))
        for index in (1, 3, 5)
    ]
    return sum(weight * channel for weight, channel in zip(
        (0.2126, 0.7152, 0.0722), channels
    ))


def _contrast(first, second):
    high, low = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


class MaterialThemeTests(unittest.TestCase):
    def test_uses_blue_tonal_spot_source(self):
        self.assertEqual(BLUE_TONAL_SPOT["variant"], "tonalSpot")
        self.assertEqual(BLUE_TONAL_SPOT["source_color"], "#0061A4")

    def test_light_and_dark_schemes_follow_material_color_roles(self):
        for mode in ("light", "dark"):
            with self.subTest(mode=mode):
                self.assertTrue(
                    REQUIRED_COLOR_ROLES.issubset(BLUE_TONAL_SPOT[mode])
                )

    def test_primary_and_container_text_pairs_have_readable_contrast(self):
        for mode in ("light", "dark"):
            scheme = BLUE_TONAL_SPOT[mode]
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
            for background, foreground in pairs:
                with self.subTest(mode=mode, pair=(background, foreground)):
                    self.assertGreaterEqual(
                        _contrast(scheme[background], scheme[foreground]),
                        4.5,
                    )


if __name__ == "__main__":
    unittest.main()
