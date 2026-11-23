import unittest
from pathlib import Path

from sun_valley_theme import _sun_valley_palette, apply_tk_theme


class SunValleyThemeTests(unittest.TestCase):
    def test_palette_matches_official_sun_valley_colors(self):
        dark = _sun_valley_palette("dark")
        light = _sun_valley_palette("light")
        self.assertEqual(dark["background"], "#1c1c1c")
        self.assertEqual(dark["foreground"], "#fafafa")
        self.assertEqual(dark["accent"], "#57c8ff")
        self.assertEqual(dark["selectionBackground"], "#2f60d8")
        self.assertEqual(dark["mutedForeground"], "#595959")
        self.assertEqual(dark["outline"], "#595959")
        self.assertEqual(light["background"], "#fafafa")
        self.assertEqual(light["foreground"], "#1c1c1c")
        self.assertEqual(light["accent"], "#005fb8")
        self.assertEqual(light["mutedForeground"], "#a0a0a0")
        self.assertEqual(light["outline"], "#a0a0a0")

    def test_unknown_theme_is_rejected(self):
        with self.assertRaises(ValueError):
            apply_tk_theme(None, "automatic")

    def test_standard_interface_widgets_use_ttk(self):
        source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        for widget in ("Frame", "Label", "Button", "Entry", "Checkbutton", "Scale"):
            self.assertNotRegex(source, rf"(?<!t)tk\.{widget}\(")
        for themed_widget in (
            "ttk.Frame(", "ttk.Label(", "ttk.Button(", "ttk.Entry(",
            "ttk.Combobox(", "ttk.Checkbutton(", "ttk.Scale(",
        ):
            self.assertIn(themed_widget, source)
        self.assertNotIn("SurfaceFrame", source)
        self.assertNotIn("FlatButton", source)
        self.assertNotIn("native_widget_classes", source)

    def test_only_non_ttk_controls_are_kept_as_native_tk_widgets(self):
        source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        self.assertIn("tk.Canvas(", source)
        self.assertIn("tk.Listbox(", source)
        self.assertIn("tk.Text(", source)
        self.assertIn('bg=palette["background"]', source)
        self.assertIn('selectbackground=palette["selectionBackground"]', source)

    def test_image_labels_use_ttk(self):
        source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        self.assertIn("label = ttk.Label(", source)


if __name__ == "__main__":
    unittest.main()
