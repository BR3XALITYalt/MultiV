import socket
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
            EmbeddedIcecastServer(mount="live.mp3")
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

    def test_listener_queue_drops_oldest_packet_when_slow(self):
        listener = _Listener(max_queue=2)
        listener.publish(b"one")
        listener.publish(b"two")
        listener.publish(b"three")
        self.assertEqual(listener.packets.get_nowait(), b"two")
        self.assertEqual(listener.packets.get_nowait(), b"three")


if __name__ == "__main__":
    unittest.main()
