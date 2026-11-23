import ast
import unittest
from pathlib import Path


MAIN_FILE = Path(__file__).resolve().parents[1] / "main.py"


class NativeTkInterfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = MAIN_FILE.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)

    def test_external_theme_dependency_is_removed(self):
        for name in ("material_theme", "sv_ttk", "SurfaceFrame", "FlatButton"):
            with self.subTest(name=name):
                self.assertNotIn(name, self.source)

    def test_app_buttons_use_native_tk_button(self):
        helper = next(
            node for node in ast.walk(self.tree)
            if isinstance(node, ast.FunctionDef) and node.name == "make_colored_button"
        )
        self.assertTrue(
            any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "tk"
                and node.func.attr == "Button"
                for node in ast.walk(helper)
            )
        )

    def test_custom_ttk_styles_are_not_required(self):
        for name in ("Switch.TCheckbutton", "Accent.TButton", "TXITY.Horizontal.TProgressbar"):
            with self.subTest(name=name):
                self.assertNotIn(name, self.source)

    def test_native_palette_supports_light_and_dark_modes(self):
        function = next(
            node for node in ast.walk(self.tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_application_palette"
        )
        source = ast.unparse(function)
        self.assertIn('"dark"', source)
        self.assertIn('"light"', source)


if __name__ == "__main__":
    unittest.main()
