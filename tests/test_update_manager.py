import tempfile
import unittest
from pathlib import Path
from unittest import mock

from update_manager import (
    _expected_filename,
    _schedule_windows_update,
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
            "version": "0.1.0-dev",
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
        self.assertEqual(update.version, "0.1.0-dev")
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

    def test_windows_manifest_accepts_inno_setup_installer(self):
        filename = "MultiV-windows-x86_64.exe"
        manifest = {
            "channel": "rolling",
            "version": "0.1.0-dev",
            "build_number": 42,
            "commit": "a" * 40,
            "assets": {
                "windows-x86_64": {
                    "filename": filename,
                    "url": (
                        "https://github.com/BR3XALITYalt/MultiV/releases/download/"
                        f"rolling/{filename}"
                    ),
                    "sha256": "b" * 64,
                }
            },
        }
        update = parse_update_manifest(
            manifest, key="windows-x86_64", current_build=41
        )
        self.assertIsNotNone(update)
        self.assertEqual(update.filename, filename)

    def test_expected_filenames_are_stable_per_platform(self):
        self.assertEqual(
            _expected_filename("windows-x86_64"),
            "MultiV-windows-x86_64.exe",
        )
        self.assertEqual(
            _expected_filename("linux-x86_64"),
            "MultiV-linux-x86_64.AppImage",
        )

    def test_windows_installer_forces_close_and_does_not_restart_locking_apps(self):
        installer = Path(__file__).resolve().parents[1] / "MultiV.iss"
        source = installer.read_text(encoding="utf-8")
        self.assertIn("CloseApplications=force", source)
        self.assertIn("CloseApplicationsFilter=*.*", source)
        self.assertIn("RestartApplications=no", source)

    def test_windows_handoff_waits_for_app_exit_then_runs_installer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            powershell = (
                root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
            )
            powershell.parent.mkdir(parents=True)
            powershell.touch()
            installer = root / "MultiV-installer.exe"
            installer.touch()
            process = mock.Mock()
            process.poll.return_value = None

            with (
                mock.patch.dict("os.environ", {"SystemRoot": temporary}),
                mock.patch("update_manager.tempfile.gettempdir", return_value=temporary),
                mock.patch("update_manager.subprocess.Popen", return_value=process) as popen,
                mock.patch("update_manager.time.sleep"),
            ):
                _schedule_windows_update(installer)

            command = popen.call_args.args[0]
            self.assertEqual(command[0], str(powershell))
            self.assertIn("-File", command)
            script_path = Path(command[-1])
            script = script_path.read_text(encoding="utf-8")
            self.assertIn("$Parent.WaitForExit(120000)", script)
            self.assertIn("Start-Process -FilePath $Installer", script)
            self.assertIn("-Verb RunAs -Wait -PassThru", script)
            self.assertIn("$InstallerArguments = ", script)
            self.assertIn("/NORESTART /SP- /CLOSEAPPLICATIONS /FORCECLOSEAPPLICATIONS", script)
            self.assertIn("/NORESTARTAPPLICATIONS /LOGCLOSEAPPLICATIONS", script)
            self.assertNotIn("/VERYSILENT", script)
            self.assertNotIn("/SUPPRESSMSGBOXES", script)
            self.assertIn("UPDATE FAILED:", script)
            self.assertIn("$OriginalExecutable", script)
            script_path.unlink(missing_ok=True)

    def test_windows_handoff_fails_before_close_if_helper_will_not_start(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            powershell = (
                root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
            )
            powershell.parent.mkdir(parents=True)
            powershell.touch()
            installer = root / "MultiV-installer.exe"
            installer.touch()
            process = mock.Mock()
            process.poll.return_value = 17

            with (
                mock.patch.dict("os.environ", {"SystemRoot": temporary}),
                mock.patch("update_manager.tempfile.gettempdir", return_value=temporary),
                mock.patch("update_manager.subprocess.Popen", return_value=process),
                mock.patch("update_manager.time.sleep"),
            ):
                with self.assertRaisesRegex(RuntimeError, "exited immediately with code 17"):
                    _schedule_windows_update(installer)

            self.assertFalse(installer.exists())


if __name__ == "__main__":
    unittest.main()
