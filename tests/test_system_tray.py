import sys
import types
import unittest
from unittest.mock import patch

from app_assets import asset_path
from PIL import Image, ImageOps
from system_tray import create_tray_icon, create_tray_image


class FakeMenuItem:
    def __init__(self, text, action, **options):
        self.text = text
        self.action = action
        self.options = options


class FakeMenu:
    SEPARATOR = object()

    def __init__(self, *items):
        self.items = items


class FakeIcon:
    def __init__(self, name, icon, *, title, menu):
        self.name = name
        self.image = icon
        self.title = title
        self.menu = menu


class SystemTrayTests(unittest.TestCase):
    def test_tray_image_uses_repository_logo(self):
        image = create_tray_image()

        with Image.open(asset_path("logo.png")) as source:
            logo = ImageOps.contain(
                source.convert("RGBA"),
                (64, 64),
                Image.Resampling.LANCZOS,
            )
            expected = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            expected.alpha_composite(
                logo,
                ((64 - logo.width) // 2, (64 - logo.height) // 2),
            )

        self.assertEqual(image.size, (64, 64))
        self.assertEqual(image.mode, "RGBA")
        self.assertEqual(image.tobytes(), expected.tobytes())

    def test_left_click_restores_and_context_menu_quits(self):
        fake_pystray = types.SimpleNamespace(
            Icon=FakeIcon,
            Menu=FakeMenu,
            MenuItem=FakeMenuItem,
        )
        restored = []
        quit_requested = []

        with patch.dict(sys.modules, {"pystray": fake_pystray}):
            icon = create_tray_icon(
                on_restore=lambda: restored.append(True),
                on_quit=lambda: quit_requested.append(True),
            )

        self.assertEqual(icon.name, "MultiV")
        self.assertEqual(icon.title, "MultiV")
        open_item, separator, quit_item = icon.menu.items
        self.assertEqual(open_item.text, "Open MultiV")
        self.assertTrue(open_item.options["default"])
        self.assertIs(separator, FakeMenu.SEPARATOR)
        self.assertEqual(quit_item.text, "Quit MultiV")

        open_item.action(icon, open_item)
        quit_item.action(icon, quit_item)

        self.assertEqual(restored, [True])
        self.assertEqual(quit_requested, [True])


if __name__ == "__main__":
    unittest.main()
