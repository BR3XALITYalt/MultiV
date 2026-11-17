import unittest

from web_gui import create_password_record, verify_password_record


class WebGUIPasswordTests(unittest.TestCase):
    def test_password_record_round_trip(self):
        salt, digest = create_password_record("a-long-test-password")
        self.assertNotEqual(salt, digest)
        self.assertTrue(
            verify_password_record("a-long-test-password", salt, digest)
        )

    def test_wrong_password_is_rejected(self):
        salt, digest = create_password_record("a-long-test-password")
        self.assertFalse(verify_password_record("not-the-password", salt, digest))

    def test_password_requires_twelve_characters(self):
        with self.assertRaises(ValueError):
            create_password_record("too-short")

    def test_malformed_password_record_is_rejected(self):
        self.assertFalse(verify_password_record("some-password", "bad-salt", "bad-hash"))
        self.assertFalse(verify_password_record("some-password", "", ""))


if __name__ == "__main__":
    unittest.main()
