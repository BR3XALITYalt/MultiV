import base64
import json
import tempfile
import time
import unittest
from pathlib import Path, PosixPath
from queue import Queue
from unittest.mock import patch

from discord_auth import (
    AUTH_START_URL,
    _browser_helper_command,
    auth_token_path,
    DiscordAuthManager,
    clean_discord_user,
    clear_auth_token,
    format_exception_details,
    is_auth_token,
    safe_error_text,
    save_auth_token,
    user_from_txity_token,
)
from discord_auth_browser import (
    AuthBridge,
    token_from_storage,
    write_diagnostic_text,
)


def fake_txity_token(payload):
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return encoded + ".signature"


class DiscordAuthValidationTests(unittest.TestCase):
    def test_browser_helper_uses_python_script_in_source_runs(self):
        command = _browser_helper_command(
            Path("discord_auth_browser.py"),
            "https://txity.example/login",
            "http://127.0.0.1:1234/complete",
            "test-state",
            False,
        )
        self.assertEqual(
            command,
            [
                __import__("sys").executable,
                "-u",
                "discord_auth_browser.py",
                "https://txity.example/login",
                "http://127.0.0.1:1234/complete",
                "test-state",
                "login",
            ],
        )

    def test_browser_helper_uses_dedicated_executable_when_frozen(self):
        with tempfile.TemporaryDirectory() as directory:
            app_executable = Path(directory) / "MultiV.exe"
            helper_executable = Path(directory) / "MultiVBrowserHelper.exe"
            app_executable.touch()
            helper_executable.touch()
            with (
                patch("discord_auth.sys.frozen", True, create=True),
                patch("discord_auth.sys.executable", str(app_executable)),
            ):
                command = _browser_helper_command(
                    Path(directory) / "discord_auth_browser.py",
                    "https://txity.example/login",
                    "http://127.0.0.1:1234/complete",
                    "test-state",
                    True,
                )

        self.assertEqual(
            command,
            [
                str(helper_executable.resolve()),
                "https://txity.example/login",
                "http://127.0.0.1:1234/complete",
                "test-state",
                "restore",
            ],
        )

    def test_browser_helper_fails_clearly_when_packaged_helper_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            app_executable = Path(directory) / "MultiV.exe"
            app_executable.touch()
            with (
                patch("discord_auth.sys.frozen", True, create=True),
                patch("discord_auth.sys.executable", str(app_executable)),
                self.assertRaisesRegex(FileNotFoundError, "MultiVBrowserHelper.exe"),
            ):
                _browser_helper_command(
                    Path(directory) / "discord_auth_browser.py",
                    "https://txity.example/login",
                    "http://127.0.0.1:1234/complete",
                    "test-state",
                    False,
                )

    def test_accepts_opaque_token(self):
        self.assertTrue(is_auth_token("signed.txity.token-value"))

    def test_windows_auth_token_uses_programdata(self):
        with patch("discord_auth.os.name", "nt"), patch(
            "discord_auth.Path", PosixPath
        ), patch.dict(
            "discord_auth.os.environ",
            {"PROGRAMDATA": r"C:\ProgramData"},
        ):
            token_path = auth_token_path()

        self.assertEqual(
            str(token_path).replace("\\", "/"),
            "C:/ProgramData/TXITPY/token.txt",
        )

    def test_auth_token_is_saved_and_cleared_from_shared_path(self):
        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory) / "TXITPY" / "token.txt"
            with patch("discord_auth.auth_token_path", return_value=token_path):
                saved_path = save_auth_token("signed.txity.token-value")
                self.assertEqual(saved_path, token_path)
                self.assertEqual(
                    token_path.read_text(encoding="utf-8"),
                    "signed.txity.token-value\n",
                )
                self.assertTrue(clear_auth_token())
                self.assertFalse(token_path.exists())

    def test_auth_callback_persists_token_before_emitting_success(self):
        token = fake_txity_token({
            "id": "123456789",
            "username": "example",
        })
        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory) / "TXITPY" / "token.txt"
            events = Queue()
            manager = DiscordAuthManager(events)
            manager._state = "test-state"
            with patch("discord_auth.auth_token_path", return_value=token_path):
                accepted = manager._complete(
                    "test-state",
                    token,
                    {"id": "123456789", "username": "example"},
                    False,
                )

            self.assertTrue(accepted)
            self.assertEqual(token_path.read_text(encoding="utf-8").strip(), token)
            event = events.get_nowait()
            self.assertEqual(event["kind"], "auth_token")
            self.assertEqual(event["token"], token)

    def test_rejects_empty_short_and_non_string_tokens(self):
        self.assertFalse(is_auth_token(""))
        self.assertFalse(is_auth_token("short"))
        self.assertFalse(is_auth_token(None))

    def test_rejects_whitespace_and_control_characters(self):
        self.assertFalse(is_auth_token("a" * 20 + "\n"))
        self.assertFalse(is_auth_token("a" * 20 + "\x00"))

    def test_rejects_oversized_tokens(self):
        self.assertFalse(is_auth_token("a" * 8193))

    def test_sanitizes_discord_identity(self):
        user = clean_discord_user({
            "id": "123456789",
            "username": "example",
            "globalName": "Example User",
            "avatarUrl": "https://cdn.example/avatar.png",
            "admin": True,
        })
        self.assertEqual(user, {
            "id": "123456789",
            "username": "example",
            "globalName": "Example User",
            "avatarUrl": "https://cdn.example/avatar.png",
        })

    def test_rejects_incomplete_discord_identity(self):
        self.assertIsNone(clean_discord_user({"id": "123"}))
        self.assertIsNone(clean_discord_user(["not", "a", "user"]))

    def test_reads_txity_first_segment_claims(self):
        token = fake_txity_token({
            "id": "123456789",
            "username": "example",
            "globalName": "Example User",
        })
        self.assertEqual(user_from_txity_token(token)["username"], "example")

    def test_rejects_expired_txity_token(self):
        token = fake_txity_token({
            "id": "123456789",
            "username": "example",
            "exp": time.time() - 60,
        })
        self.assertIsNone(user_from_txity_token(token))


    def test_accepts_session_only_from_txity_origin(self):
        token = fake_txity_token({
            "id": "123456789",
            "username": "example",
        })
        self.assertEqual(
            token_from_storage("https://txity.radiodiscussion.org", token),
            token,
        )

    def test_rejects_session_from_other_origins(self):
        token = fake_txity_token({
            "id": "123456789",
            "username": "example",
        })
        self.assertIsNone(token_from_storage("https://discord.com", token))
        self.assertIsNone(token_from_storage("http://txity.radiodiscussion.org", token))

    def test_rejects_expired_session_from_txity_storage(self):
        token = fake_txity_token({
            "id": "123456789",
            "username": "example",
            "exp": time.time() - 60,
        })
        self.assertIsNone(token_from_storage("https://txity.radiodiscussion.org", token))


    def test_error_diagnostics_redact_urls_and_credentials(self):
        error = RuntimeError(
            "failed https://example.test/path?state=secretvalue "
            "authToken=abcdef0123456789abcdef0123456789"
        )
        message = safe_error_text(error)
        self.assertNotIn("secretvalue", message)
        self.assertNotIn("abcdef0123456789abcdef0123456789", message)
        self.assertIn("[URL redacted]", message)
        self.assertIn("authToken=[redacted]", message)

    def test_verbose_exception_details_include_traceback_and_redact_secrets(self):
        try:
            raise RuntimeError(
                "failed https://example.test/path?state=secretvalue "
                'authToken="abcdef0123456789abcdef0123456789"'
            )
        except RuntimeError as exc:
            details = format_exception_details(exc)

        self.assertIn("Traceback (most recent call last)", details)
        self.assertIn("RuntimeError", details)
        self.assertIn("line", details)
        self.assertNotIn("secretvalue", details)
        self.assertNotIn("abcdef0123456789abcdef0123456789", details)
        self.assertIn("[URL redacted]", details)
        self.assertIn("authToken=[redacted]", details)

    def test_safe_error_text_preserves_line_breaks(self):
        details = safe_error_text("first line\nsecond line")
        self.assertIn("first line\nsecond line", details)

    def test_browser_imports_diagnostic_text_writer(self):
        self.assertTrue(callable(write_diagnostic_text))

    def test_native_window_reference_is_private_from_js_api(self):
        bridge = AuthBridge("http://127.0.0.1:12345/complete", "test-state")

        self.assertFalse(hasattr(bridge, "window"))
        self.assertTrue(hasattr(bridge, "_window"))
        self.assertNotIn("window", vars(bridge))
        self.assertIn("_window", vars(bridge))

    def test_restore_missing_requires_matching_restore_request(self):
        manager = DiscordAuthManager(Queue())
        manager._state = "restore-state"
        manager._restore_only = True

        self.assertFalse(manager._restore_missing("wrong-state"))
        self.assertTrue(manager._restore_missing("restore-state"))
        self.assertEqual(
            manager.events.get_nowait(),
            {"kind": "auth_restore_missing"},
        )
        self.assertFalse(manager._restore_missing("restore-state"))

    def test_restore_missing_is_not_used_for_regular_login(self):
        manager = DiscordAuthManager(Queue())
        manager._state = "login-state"

        self.assertFalse(manager._restore_missing("login-state"))
        self.assertTrue(manager.events.empty())

    def test_auth_route_defaults_to_txity_route(self):
        manager = DiscordAuthManager(Queue())
        manager.configure(None, True)
        self.assertEqual(manager.auth_start_url, AUTH_START_URL)
        self.assertTrue(manager.secure_auth_enabled)

    def test_rejects_non_txity_auth_routes(self):
        manager = DiscordAuthManager(Queue())
        manager.configure("https://attacker.example/login", True)
        self.assertFalse(manager.secure_auth_enabled)
        manager.configure("http://txity.radiodiscussion.org/login", True)
        self.assertFalse(manager.secure_auth_enabled)

    def test_explicit_blank_auth_route_disables_login(self):
        manager = DiscordAuthManager(Queue())
        manager.configure("", True)
        self.assertFalse(manager.secure_auth_enabled)

    def test_requires_server_secure_auth_flag(self):
        manager = DiscordAuthManager(Queue())
        manager.configure("/auth/discord/start", False)
        self.assertFalse(manager.secure_auth_enabled)


if __name__ == "__main__":
    unittest.main()
