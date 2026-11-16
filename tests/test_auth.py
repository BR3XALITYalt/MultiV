import base64
import json
import time
import unittest

from discord_auth import clean_discord_user, is_auth_token, user_from_txity_token


def fake_txity_token(payload):
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return encoded + ".signature"


class DiscordAuthValidationTests(unittest.TestCase):
    def test_accepts_opaque_token(self):
        self.assertTrue(is_auth_token("signed.txity.token-value"))

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


if __name__ == "__main__":
    unittest.main()
