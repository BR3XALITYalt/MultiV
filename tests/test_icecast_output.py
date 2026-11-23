import base64
import queue
import unittest
from unittest import mock

from icecast_output import (
    ICECAST_BITRATES,
    IcecastBroadcaster,
    _Destination,
    normalize_icecast_destination,
)


class FakeSocket:
    def __init__(self, response=b"HTTP/1.0 200 OK\r\n\r\n"):
        self.response = response
        self.sent = bytearray()
        self.timeout = None
        self.closed = False

    def settimeout(self, value):
        self.timeout = value

    def sendall(self, payload):
        self.sent.extend(payload)

    def recv(self, _size):
        response, self.response = self.response, b""
        return response

    def close(self):
        self.closed = True

    def shutdown(self, _how):
        pass


class IcecastDestinationTests(unittest.TestCase):
    def test_normalizes_http_destination(self):
        result = normalize_icecast_destination({
            "url": "http://radio.example:8080/live.mp3",
            "username": "source",
            "password": "secret",
        })
        self.assertEqual(result["host"], "radio.example")
        self.assertEqual(result["port"], "8080")
        self.assertEqual(result["mount"], "/live.mp3")
        self.assertEqual(result["scheme"], "http")
        self.assertEqual(result["name"], "radio.example:8080/live.mp3")

    def test_uses_default_ports(self):
        http = normalize_icecast_destination({
            "url": "http://radio.example/live",
            "password": "secret",
        })
        https = normalize_icecast_destination({
            "url": "https://radio.example/live",
            "password": "secret",
        })
        self.assertEqual(http["port"], "8000")
        self.assertEqual(https["port"], "443")

    def test_supports_ipv6_hosts(self):
        result = normalize_icecast_destination({
            "url": "https://[2001:db8::1]:8443/radio",
            "password": "secret",
        })
        self.assertEqual(result["host"], "2001:db8::1")
        self.assertEqual(result["name"], "[2001:db8::1]:8443/radio")

    def test_rejects_invalid_destination_values(self):
        invalid = (
            {},
            {"url": "ftp://radio.example/live", "password": "secret"},
            {"url": "http://user:pass@radio.example/live", "password": "secret"},
            {"url": "http://radio.example/live?x=1", "password": "secret"},
            {"url": "http://radio.example/", "password": "secret"},
            {"url": "http://radio.example/live", "password": ""},
            {"url": "http://radio.example/live", "password": "bad\r\nheader"},
            {"url": "http://radio.example/live", "username": "bad:name", "password": "secret"},
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    normalize_icecast_destination(value)

    def test_connect_sends_source_request_and_basic_auth(self):
        fake = FakeSocket()
        destination = _Destination(
            normalize_icecast_destination({
                "url": "http://radio.example:8000/live.mp3",
                "username": "source",
                "password": "secret",
            }),
            queue.Queue(),
            "Station",
            "Station description",
        )
        with mock.patch("icecast_output.socket.create_connection", return_value=fake):
            connection = destination.connect()

        self.assertIs(connection, fake)
        request = fake.sent.decode("utf-8")
        self.assertIn("SOURCE /live.mp3 HTTP/1.0", request)
        expected = base64.b64encode(b"source:secret").decode("ascii")
        self.assertIn(f"Authorization: Basic {expected}", request)
        self.assertIn("ice-name: Station", request)
        self.assertIn("ice-description: Station description", request)

    def test_normalizes_butt_style_formats(self):
        cases = {
            "mp3": ("audio/mpeg", 128),
            "ogg_vorbis": ("application/ogg", 128),
            "ogg_opus": ("application/ogg", 128),
            "ogg_flac": ("application/ogg", 0),
            "aac_plus": ("audio/aacp", 96),
        }
        for audio_format, (content_type, bitrate) in cases.items():
            with self.subTest(audio_format=audio_format):
                result = normalize_icecast_destination({
                    "url": "http://radio.example/live",
                    "password": "secret",
                    "format": audio_format,
                    "bitrate": bitrate or 128,
                })
                from icecast_output import ICECAST_FORMATS
                self.assertEqual(ICECAST_FORMATS[audio_format]["content_type"], content_type)
                self.assertEqual(int(result["bitrate"]), bitrate)

    def test_rejects_invalid_codec_bitrate_combinations(self):
        with self.assertRaisesRegex(ValueError, "AAC"):
            normalize_icecast_destination({
                "url": "http://radio.example/live", "password": "secret",
                "format": "aac_plus", "bitrate": 192,
            })
        with self.assertRaisesRegex(ValueError, "Opus"):
            normalize_icecast_destination({
                "url": "http://radio.example/live", "password": "secret",
                "format": "ogg_opus", "bitrate": 32,
            })

    def test_destinations_with_different_formats_get_separate_encoder_groups(self):
        broadcaster = IcecastBroadcaster(
            [
                {"url": "http://radio-one.example/live", "password": "one",
                 "format": "mp3", "bitrate": 128},
                {"url": "http://radio-two.example/live", "password": "two",
                 "format": "ogg_opus", "bitrate": 96},
                {"url": "http://radio-three.example/live", "password": "three",
                 "format": "ogg_opus", "bitrate": 96},
            ],
            events=queue.Queue(),
        )
        self.assertEqual(
            [(group.audio_format, group.bitrate_kbps) for group in broadcaster._groups],
            [("mp3", 128), ("ogg_opus", 96)],
        )
        self.assertEqual(len(broadcaster._groups[1].destinations), 2)

    def test_rejects_non_success_server_response(self):
        fake = FakeSocket(b"HTTP/1.0 401 Unauthorized\r\n\r\n")
        destination = _Destination(
            normalize_icecast_destination({
                "url": "http://radio.example/live",
                "password": "secret",
            }),
            queue.Queue(),
            "MultiV",
            "",
        )
        with mock.patch("icecast_output.socket.create_connection", return_value=fake):
            with self.assertRaisesRegex(ConnectionError, "authentication failed"):
                destination.connect()
        self.assertTrue(fake.closed)

    def test_output_queue_drops_oldest_packet_when_full(self):
        destination = _Destination(
            normalize_icecast_destination({
                "url": "http://radio.example/live",
                "password": "secret",
            }),
            queue.Queue(),
            "MultiV",
            "",
        )
        destination.packets = queue.Queue(maxsize=2)
        destination.enqueue_packet(b"first")
        destination.enqueue_packet(b"second")
        destination.enqueue_packet(b"third")
        self.assertEqual(destination.packets.get_nowait(), b"second")
        self.assertEqual(destination.packets.get_nowait(), b"third")

    def test_encoded_packet_is_fanned_out_to_every_destination(self):
        broadcaster = IcecastBroadcaster(
            [
                {"url": "http://radio-one.example/live", "password": "one"},
                {"url": "http://radio-two.example/live", "password": "two"},
            ],
            events=queue.Queue(),
        )
        broadcaster._fanout(b"mp3-frame")
        self.assertEqual(
            [destination.packets.get_nowait() for destination in broadcaster._destinations],
            [b"mp3-frame", b"mp3-frame"],
        )

    def test_broadcaster_validates_bitrate_and_destinations(self):
        with self.assertRaisesRegex(ValueError, "at least one"):
            IcecastBroadcaster([], events=queue.Queue())
        with self.assertRaisesRegex(ValueError, "bitrate"):
            IcecastBroadcaster(
                [{"url": "http://radio.example/live", "password": "secret"}],
                bitrate_kbps=111,
                events=queue.Queue(),
            )
        for bitrate in ICECAST_BITRATES:
            broadcaster = IcecastBroadcaster(
                [{"url": "http://radio.example/live", "password": "secret"}],
                bitrate_kbps=bitrate,
                events=queue.Queue(),
            )
            self.assertEqual(broadcaster.bitrate, bitrate * 1000)

    def test_available_encoders_produce_stream_packets(self):
        import numpy as np
        from icecast_output import available_icecast_formats

        available = available_icecast_formats()
        for audio_format in available:
            with self.subTest(audio_format=audio_format):
                broadcaster = IcecastBroadcaster(
                    [{
                        "url": f"http://{audio_format}.example/live",
                        "password": "secret",
                        "format": audio_format,
                        "bitrate": 96,
                    }],
                    events=queue.Queue(),
                )
                group = broadcaster._groups[0]
                group.input_queue.put(np.zeros(512, dtype=np.float32))
                group.input_queue.put(None)
                broadcaster._encode_group(group)
                destination = broadcaster._destinations[0]
                self.assertFalse(destination.packets.empty())

    def test_send_pcm_copies_finite_mono_samples_into_bounded_queue(self):
        import numpy as np

        broadcaster = IcecastBroadcaster(
            [{"url": "http://radio.example/live", "password": "secret"}],
            events=queue.Queue(),
        )
        samples = np.array([0.25, float("nan"), float("inf")], dtype=np.float32)
        broadcaster.send_pcm(samples)
        samples[:] = 0.0
        queued = broadcaster._groups[0].input_queue.get_nowait()
        self.assertEqual(queued[0], 0.25)
        self.assertEqual(queued[1], 0.0)
        self.assertEqual(queued[2], 0.0)


if __name__ == "__main__":
    unittest.main()
