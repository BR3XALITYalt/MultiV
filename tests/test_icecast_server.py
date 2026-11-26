import socket
import threading
import time
import unittest

from icecast_server import EmbeddedIcecastServer, _Listener


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class EmbeddedIcecastServerTests(unittest.TestCase):
    def make_server(self, **kwargs):
        return EmbeddedIcecastServer(
            host="127.0.0.1",
            port=free_port(),
            mount="/station.mp3",
            **kwargs,
        )

    def test_validates_port_mount_and_listener_limit(self):
        with self.assertRaises(ValueError):
            EmbeddedIcecastServer(port=0)
        with self.assertRaises(ValueError):
            EmbeddedIcecastServer(mount="/bad mount")
        with self.assertRaises(ValueError):
            EmbeddedIcecastServer(max_listeners=0)

    def test_listener_password_uses_basic_auth(self):
        server = self.make_server(listener_password="correct horse")
        import base64

        valid = base64.b64encode(b"listener:correct horse").decode("ascii")
        invalid = base64.b64encode(b"listener:wrong").decode("ascii")
        self.assertTrue(server._authorized("Basic " + valid))
        self.assertFalse(server._authorized("Basic " + invalid))
        self.assertFalse(server._authorized("Bearer " + valid))

    def test_stop_does_not_block_forever_if_server_shutdown_stalls(self):
        server = self.make_server()
        blocked = threading.Event()

        class StalledServer:
            def shutdown(self):
                blocked.wait(5.0)

            def server_close(self):
                return None

        server._server = StalledServer()
        server._thread = None
        started = time.monotonic()
        try:
            server.stop()
            self.assertLess(time.monotonic() - started, 2.0)
        finally:
            blocked.set()

    def test_server_starts_and_stops_http_listener(self):
        server = self.make_server()
        server.start()
        try:
            with socket.create_connection(("127.0.0.1", server.port), timeout=2) as connection:
                connection.sendall(
                    b"GET /station.mp3 HTTP/1.0\r\nHost: localhost\r\n\r\n"
                )
                response = connection.recv(4096)
            self.assertIn(b"200 OK", response)
            self.assertIn(b"Content-Type: audio/mpeg", response)
            self.assertTrue(server.is_running)
        finally:
            server.stop()
        self.assertFalse(server.is_running)

    def test_server_uses_selected_ogg_codec_and_bitrate(self):
        server = self.make_server(audio_format="ogg_opus", bitrate_kbps=96)
        server.start()
        try:
            with socket.create_connection(("127.0.0.1", server.port), timeout=2) as connection:
                connection.sendall(
                    b"GET /station.mp3 HTTP/1.0\r\nHost: localhost\r\n\r\n"
                )
                response = connection.recv(4096)
            self.assertIn(b"200 OK", response)
            self.assertIn(b"Content-Type: application/ogg", response)
            self.assertIn(b"icy-br: 96", response)
        finally:
            server.stop()

    def test_lossless_server_omits_bitrate_header(self):
        server = self.make_server(audio_format="ogg_flac", bitrate_kbps=128)
        self.assertEqual(server.bitrate_kbps, 0)
        server.start()
        try:
            with socket.create_connection(("127.0.0.1", server.port), timeout=2) as connection:
                connection.sendall(
                    b"GET /station.mp3 HTTP/1.0\r\nHost: localhost\r\n\r\n"
                )
                response = connection.recv(4096)
            self.assertIn(b"Content-Type: application/ogg", response)
            self.assertNotIn(b"icy-br:", response)
        finally:
            server.stop()

    def test_validates_codec_specific_bitrate_limits(self):
        with self.assertRaisesRegex(ValueError, "AAC"):
            self.make_server(audio_format="aac_plus", bitrate_kbps=192)
        with self.assertRaisesRegex(ValueError, "Opus"):
            self.make_server(audio_format="ogg_opus", bitrate_kbps=32)

    def test_listener_queue_drops_oldest_packet_when_slow(self):
        listener = _Listener(max_queue=2)
        listener.publish(b"one")
        listener.publish(b"two")
        listener.publish(b"three")
        self.assertEqual(listener.packets.get_nowait(), b"two")
        self.assertEqual(listener.packets.get_nowait(), b"three")


if __name__ == "__main__":
    unittest.main()
