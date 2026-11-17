import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from station_upload import (
    MAX_UPLOAD_BYTES,
    UPLOAD_URL,
    UPLOAD_USER_AGENT,
    _extract_image_url,
    image_file_to_data_url,
    upload_station_image,
)
from txity_client import TxityClient


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit=-1):
        return self.body


class StationImageUploadTests(unittest.TestCase):
    def create_png(self, folder):
        image_path = Path(folder) / "logo.png"
        Image.new("RGB", (4, 4), (24, 80, 180)).save(image_path, format="PNG")
        return image_path

    def test_image_file_becomes_a_png_data_url(self):
        with tempfile.TemporaryDirectory() as folder:
            image_path = self.create_png(folder)
            data_url = image_file_to_data_url(image_path)

        self.assertTrue(data_url.startswith("data:image/png;base64,"))
        encoded = data_url.partition(",")[2]
        self.assertTrue(base64.b64decode(encoded).startswith(b"\x89PNG"))

    def test_rejects_invalid_image_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            image_path = Path(folder) / "broken.png"
            image_path.write_bytes(b"not an image")
            with self.assertRaisesRegex(ValueError, "valid PNG"):
                image_file_to_data_url(image_path)

    def test_rejects_images_larger_than_upload_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            image_path = Path(folder) / "large.png"
            image_path.write_bytes(b"x" * (MAX_UPLOAD_BYTES + 1))
            with self.assertRaisesRegex(ValueError, "smaller than 5 MB"):
                image_file_to_data_url(image_path)

    def test_upload_posts_required_session_fields_and_returns_hosted_url(self):
        data_url = "data:image/png;base64,aGVsbG8="
        token = "test-session-token-value"
        response = FakeResponse(json.dumps({
            "ok": True,
            "data": {"imageUrl": "/uploads/stations/logo-123.png"},
        }).encode("utf-8"))

        with patch("station_upload.urlopen", return_value=response) as mocked:
            result = upload_station_image(
                "293ab92c-db4a-410c-9c8b-6a90bc9b1b32",
                token,
                data_url,
            )

        request = mocked.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(request.full_url, UPLOAD_URL)
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(request.get_header("User-agent"), UPLOAD_USER_AGENT)
        self.assertEqual(request.get_header("Referer"), "https://txity.radiodiscussion.org/")
        self.assertEqual(body, {
            "sessionId": "293ab92c-db4a-410c-9c8b-6a90bc9b1b32",
            "authToken": token,
            "kind": "logo",
            "dataUrl": data_url,
        })
        self.assertEqual(
            result,
            "https://txity.radiodiscussion.org/uploads/stations/logo-123.png",
        )

    def test_rejects_upload_url_from_unapproved_host(self):
        self.assertEqual(
            _extract_image_url({"success": True, "url": "https://example.com/logo.png"}),
            "",
        )

    def test_rejects_server_declined_upload(self):
        response = FakeResponse(json.dumps({
            "success": False,
            "message": "Image rejected",
        }).encode("utf-8"))
        with patch("station_upload.urlopen", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "Image rejected"):
                upload_station_image(
                    "293ab92c-db4a-410c-9c8b-6a90bc9b1b32",
                    "test-session-token-value",
                    "data:image/png;base64,aGVsbG8=",
                )

    def test_rejects_missing_uploaded_image_url(self):
        response = FakeResponse(b'{"ok":true,"message":"Uploaded"}')
        with patch("station_upload.urlopen", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "did not return"):
                upload_station_image(
                    "293ab92c-db4a-410c-9c8b-6a90bc9b1b32",
                    "test-session-token-value",
                    "data:image/png;base64,aGVsbG8=",
                )

    def test_rejects_invalid_session_and_auth_token(self):
        with self.assertRaisesRegex(ValueError, "session ID"):
            upload_station_image(
                "not-a-session",
                "test-session-token-value",
                "data:image/png;base64,aGVsbG8=",
            )
        with self.assertRaisesRegex(RuntimeError, "Sign in with Discord"):
            upload_station_image(
                "293ab92c-db4a-410c-9c8b-6a90bc9b1b32",
                "",
                "data:image/png;base64,aGVsbG8=",
            )

    def test_client_uses_its_active_session_and_auth_token(self):
        client = TxityClient()
        token = "test-session-token-value"
        client.set_discord_auth(token, {"id": "123456789012345678"})

        with (
            patch("txity_client.image_file_to_data_url", return_value="data:image/png;base64,aGVsbG8="),
            patch(
                "txity_client.post_station_image",
                return_value="https://txity.radiodiscussion.org/uploads/logo.png",
            ) as mocked,
        ):
            client.upload_station_image_file("logo.png")
            event = client.events.get(timeout=3)

        self.assertEqual(event["kind"], "station_image_upload_success")
        self.assertEqual(event["url"], "https://txity.radiodiscussion.org/uploads/logo.png")
        self.assertEqual(mocked.call_args.args[:3], (
            client.session_id,
            token,
            "data:image/png;base64,aGVsbG8=",
        ))


if __name__ == "__main__":
    unittest.main()
