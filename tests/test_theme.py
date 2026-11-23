import unittest
from pathlib import Path

from sun_valley_theme import _sun_valley_palette, apply_tk_theme


class SunValleyThemeTests(unittest.TestCase):
    def test_palette_uses_official_sun_valley_colors(self):
        dark = _sun_valley_palette("dark")
        light = _sun_valley_palette("light")
        self.assertEqual(dark["background"], "#1c1c1c")
        self.assertEqual(dark["onSurface"], "#fafafa")
        self.assertEqual(dark["primary"], "#57c8ff")
        self.assertEqual(light["background"], "#fafafa")
        self.assertEqual(light["onSurface"], "#1c1c1c")
        self.assertEqual(light["primary"], "#005fb8")

    def test_unknown_theme_is_rejected(self):
        with self.assertRaises(ValueError):
            apply_tk_theme(None, "automatic")

    def test_main_uses_ttk_for_standard_widgets(self):
        source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        self.assertNotRegex(source, r"(?<!t)tk\.Frame\(")
        self.assertNotRegex(source, r"(?<!t)tk\.Label\(")
        for themed_widget in (
            "ttk.Frame(", "ttk.Label(", "ttk.Button(", "ttk.Entry(",
            "ttk.Combobox(", "ttk.Checkbutton(", "ttk.Scale(",
        ):
            self.assertIn(themed_widget, source)
        self.assertNotIn("SurfaceFrame", source)
        self.assertNotIn("FlatButton", source)
        self.assertNotIn("native_widget_classes", source)

    def test_image_labels_use_ttk(self):
        source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        self.assertIn("def make_image_label(", source)
        self.assertIn("label = ttk.Label(", source)

    def test_legacy_widgets_use_official_sun_valley_colors(self):
        source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        self.assertIn('bg=palette["surfaceContainerLowest"]', source)
        self.assertIn('fg=palette["onSurface"]', source)
        self.assertIn('selectbackground=palette["primaryContainer"]', source)


if __name__ == "__main__":
    unittest.main()
