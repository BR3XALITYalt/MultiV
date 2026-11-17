import http.client
import json
import re
import unittest
from urllib.parse import urlencode

from web_gui import (
    TxityWebGUI,
    create_password_record,
    verify_password_record,
)


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


class WebGUIHTTPTests(unittest.TestCase):
    def test_login_session_and_csrf_protect_transmit_commands(self):
        salt, digest = create_password_record("a-long-test-password")
        commands = []
        gui = TxityWebGUI(
            host="127.0.0.1",
            port=0,
            password_salt=salt,
            password_hash=digest,
            state_provider=lambda: {"status": "Not transmitting", "transmitting": False},
            command_submitter=commands.append,
        )
        gui.start()
        try:
            address = gui.address
            self.assertIsNotNone(address)
            host, port = address
            connection = http.client.HTTPConnection(host, port, timeout=5)

            login_body = urlencode({"password": "a-long-test-password"})
            connection.request(
                "POST",
                "/login",
                body=login_body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            login = connection.getresponse()
            self.assertEqual(login.status, 200)
            login.read()
            cookie = login.getheader("Set-Cookie").split(";", 1)[0]

            connection.request("GET", "/", headers={"Cookie": cookie})
            dashboard = connection.getresponse()
            self.assertEqual(dashboard.status, 200)
            page = dashboard.read().decode("utf-8")
            csrf_match = re.search(
                r'<meta name="csrf-token" content="([^"]+)"',
                page,
            )
            self.assertIsNotNone(csrf_match)
            csrf = csrf_match.group(1)

            connection.request(
                "POST",
                "/api/transmit",
                body=json.dumps({"action": "start"}),
                headers={
                    "Cookie": cookie,
                    "Content-Type": "application/json",
                },
            )
            blocked = connection.getresponse()
            self.assertEqual(blocked.status, 403)
            blocked.read()

            connection.request(
                "POST",
                "/api/transmit",
                body=json.dumps({"action": "start"}),
                headers={
                    "Cookie": cookie,
                    "Content-Type": "application/json",
                    "X-CSRF-Token": csrf,
                },
            )
            accepted = connection.getresponse()
            self.assertEqual(accepted.status, 202)
            accepted.read()
            self.assertEqual(
                commands,
                [{"type": "transmit", "action": "start"}],
            )
            connection.close()
        finally:
            gui.stop()


if __name__ == "__main__":
    unittest.main()
