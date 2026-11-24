import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from multiv_theme import (
    _multiv_palette,
    apply_ttkbootstrap_theme,
    register_ttkbootstrap_themes,
)


class RecordingStyle:
    def __init__(self):
        self.active_theme = None

    def theme_use(self, name):
        self.active_theme = name


class RecordingRoot:
    def __init__(self):
        self.options = {}

    def configure(self, **options):
        self.options.update(options)


class MultiVThemeTests(unittest.TestCase):
    def test_palette_preserves_sun_valley_colors(self):
        dark = _multiv_palette("dark")
        light = _multiv_palette("light")
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
            apply_ttkbootstrap_theme(None, RecordingStyle(), "automatic")

    def test_registers_sun_valley_compatible_light_dark_theme_family(self):
        theme = Mock()
        with patch("ttkbootstrap.Theme", return_value=theme) as theme_factory:
            result = register_ttkbootstrap_themes()

        theme_factory.assert_called_once_with(
            name="multiv",
            primary="#57c8ff",
            secondary="#595959",
            success="#107c10",
            info="#57c8ff",
            warning="#ffb900",
            danger="#d13438",
            neutral="#595959",
            light={"background": "#fafafa", "foreground": "#1c1c1c"},
            dark={"background": "#1c1c1c", "foreground": "#fafafa"},
        )
        theme.register.assert_called_once_with()
        self.assertIs(result, theme.register.return_value)

    def test_apply_switches_theme_and_sets_root_surface(self):
        style = RecordingStyle()
        root = RecordingRoot()
        palette = apply_ttkbootstrap_theme(root, style, "dark")
        self.assertEqual(style.active_theme, "multiv-dark")
        self.assertEqual(root.options["background"], "#1c1c1c")
        self.assertEqual(palette["accent"], "#57c8ff")

    def test_ttkbootstrap_style_uses_its_supported_constructor(self):
        source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        root_creation = source.index("root = tk.Tk()")
        style_creation = source.index("theme_style = ttk.Style()")
        self.assertLess(root_creation, style_creation)
        self.assertNotIn("ttk.Style(master=root)", source)

    def test_fatal_startup_errors_are_visible_and_fail_nonzero(self):
        source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        self.assertIn('messagebox.showerror("MultiV could not start", summary)', source)
        self.assertIn("raise SystemExit(1) from exc", source)

    def test_standard_widgets_use_ttkbootstrap(self):
        source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        self.assertIn("import ttkbootstrap as ttk", source)
        for widget in (
            "ttk.Frame(", "ttk.Label(", "ttk.Button(", "ttk.Entry(",
            "ttk.Combobox(", "ttk.Checkbutton(", "ttk.Scale(",
            "ttk.Canvas(", "ttk.Listbox(", "ttk.Text(",
            "ttk.Toplevel(", "ttk.Menu(",
        ):
            self.assertIn(widget, source)
        self.assertIn('bootstyle="primary"', source)
        self.assertNotIn("sv_ttk", source)
        self.assertNotIn("sun_valley_theme", source)

    def test_classic_tk_is_retained_for_variables_and_runtime_primitives(self):
        source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        self.assertIn("root = tk.Tk()", source)
        self.assertIn("tk.StringVar(", source)
        self.assertIn("tk.BooleanVar(", source)
        for widget in ("Canvas", "Listbox", "Text", "Toplevel", "Menu"):
            self.assertNotRegex(source, rf"(?<!t)tk\.{widget}\(")

    def test_image_labels_use_ttkbootstrap(self):
        source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        self.assertIn("label = ttk.Label(", source)


if __name__ == "__main__":
    unittest.main()
