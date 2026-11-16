import sys
import types
import unittest
from unittest.mock import patch

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
    def test_tray_image_has_expected_size_and_transparency(self):
        image = create_tray_image()

        self.assertEqual(image.size, (64, 64))
        self.assertEqual(image.mode, "RGBA")
        self.assertEqual(image.getpixel((0, 0))[3], 0)
        self.assertEqual(image.getpixel((32, 32)), (255, 255, 255, 255))

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

        self.assertEqual(icon.name, "TXITPY")
        self.assertEqual(icon.title, "TXITPY")
        open_item, separator, quit_item = icon.menu.items
        self.assertEqual(open_item.text, "Open TXITPY")
        self.assertTrue(open_item.options["default"])
        self.assertIs(separator, FakeMenu.SEPARATOR)
        self.assertEqual(quit_item.text, "Quit TXITPY")

        open_item.action(icon, open_item)
        quit_item.action(icon, quit_item)

        self.assertEqual(restored, [True])
        self.assertEqual(quit_requested, [True])


if __name__ == "__main__":
    unittest.main()
