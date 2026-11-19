import unittest
from unittest import mock

from update_manager import (
    _expected_filename,
    is_packaged,
    parse_update_manifest,
    platform_key,
)


class UpdateManagerTests(unittest.TestCase):
    def manifest(self, *, filename="MultiV-linux-x86_64.AppImage", url=None):
        if url is None:
            url = (
                "https://github.com/BR3XALITYalt/MultiV/releases/download/"
                f"rolling/{filename}"
            )
        return {
            "channel": "rolling",
            "version": "0.1.0-dev.42",
            "build_number": 42,
            "commit": "a" * 40,
            "assets": {
                "linux-x86_64": {
                    "filename": filename,
                    "url": url,
                    "sha256": "b" * 64,
                }
            },
        }

    def test_platform_key_selects_supported_binary(self):
        with (
            mock.patch("update_manager.sys.platform", "linux"),
            mock.patch("update_manager.platform.machine", return_value="x86_64"),
        ):
            self.assertEqual(platform_key(), "linux-x86_64")
        with (
            mock.patch("update_manager.sys.platform", "win32"),
            mock.patch("update_manager.platform.machine", return_value="AMD64"),
        ):
            self.assertEqual(platform_key(), "windows-x86_64")

    def test_platform_key_rejects_unsupported_architecture(self):
        with (
            mock.patch("update_manager.sys.platform", "linux"),
            mock.patch("update_manager.platform.machine", return_value="aarch64"),
        ):
            self.assertIsNone(platform_key())

    def test_packaged_detection_uses_pyinstaller_flag(self):
        with mock.patch.object(__import__("sys"), "frozen", True, create=True):
            self.assertTrue(is_packaged())
        with mock.patch.object(__import__("sys"), "frozen", False, create=True):
            self.assertFalse(is_packaged())

    def test_valid_newer_build_is_returned(self):
        update = parse_update_manifest(
            self.manifest(),
            key="linux-x86_64",
            current_build=41,
            release_url="https://github.com/BR3XALITYalt/MultiV/releases/tag/rolling",
        )
        self.assertIsNotNone(update)
        self.assertEqual(update.version, "0.1.0-dev.42")
        self.assertEqual(update.build_number, 42)
        self.assertEqual(update.filename, "MultiV-linux-x86_64.AppImage")
        self.assertEqual(update.sha256, "b" * 64)

    def test_same_or_older_build_is_not_an_update(self):
        self.assertIsNone(
            parse_update_manifest(
                self.manifest(),
                key="linux-x86_64",
                current_build=42,
            )
        )

    def test_wrong_release_channel_is_rejected(self):
        manifest = self.manifest()
        manifest["channel"] = "stable"
        with self.assertRaisesRegex(RuntimeError, "not a rolling release"):
            parse_update_manifest(manifest, key="linux-x86_64", current_build=1)

    def test_unexpected_asset_filename_is_rejected(self):
        manifest = self.manifest(filename="not-multiv.AppImage")
        with self.assertRaisesRegex(RuntimeError, "unexpected application package"):
            parse_update_manifest(manifest, key="linux-x86_64", current_build=1)

    def test_unexpected_download_url_is_rejected(self):
        manifest = self.manifest(url="https://example.invalid/MultiV.AppImage")
        with self.assertRaisesRegex(RuntimeError, "unexpected download URL"):
            parse_update_manifest(manifest, key="linux-x86_64", current_build=1)

    def test_invalid_sha256_is_rejected(self):
        manifest = self.manifest()
        manifest["assets"]["linux-x86_64"]["sha256"] = "not-a-hash"
        with self.assertRaisesRegex(RuntimeError, "invalid SHA-256"):
            parse_update_manifest(manifest, key="linux-x86_64", current_build=1)

    def test_missing_platform_asset_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "missing required fields"):
            parse_update_manifest(
                {
                    "channel": "rolling",
                    "version": "0.1.1",
                    "build_number": 43,
                    "commit": "a" * 40,
                    "assets": {},
                },
                key="linux-x86_64",
                current_build=1,
            )

    def test_expected_filenames_are_fixed_per_platform(self):
        self.assertEqual(_expected_filename("windows-x86_64"), "MultiV-windows-x86_64.zip")
        self.assertEqual(_expected_filename("linux-x86_64"), "MultiV-linux-x86_64.AppImage")


if __name__ == "__main__":
    unittest.main()
