from __future__ import annotations

import json
import socket
import struct
import unittest
from typing import Any

from simtx_api import (
    AUTH_KIND_GAME_TICKET,
    HELLO_STATUS_ACCEPTED,
    ROLE_TX_AUDIO,
    SESSION_STATE_BOUND,
    SimTxAccountClient,
    SimTxApiError,
    SimTxCredentials,
    SimTxDeviceAuthorization,
    SimTxGameTicket,
    SimTxServer,
    build_audio_sample_block,
    build_client_hello,
    build_transmitter_start,
    decode_proto_fields,
    decode_varint,
    encode_delimited_frame,
    encode_varint,
    proto_bytes,
    proto_fixed32,
    proto_fixed64,
    proto_varint,
    read_delimited_frame,
    wait_for_device_login,
)


class FakeResponse:
    def __init__(self, value: dict[str, Any], status: int = 200) -> None:
        self.raw = json.dumps(value).encode("utf-8")
        self.status = status
        self.headers = {}

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def read(self, limit: int = -1) -> bytes:
        return self.raw if limit < 0 else self.raw[:limit]


class SimTxProtobufTests(unittest.TestCase):
    def test_varint_round_trip(self) -> None:
        for value in (0, 1, 127, 128, 300, 16_384, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF):
            encoded = encode_varint(value)
            decoded, offset = decode_varint(encoded)
            self.assertEqual(decoded, value)
            self.assertEqual(offset, len(encoded))

    def test_varint_rejects_truncation_and_overflow(self) -> None:
        with self.assertRaises(ValueError):
            decode_varint(b"\x80")
        with self.assertRaises(ValueError):
            encode_varint(-1)
        with self.assertRaises(ValueError):
            decode_varint(b"\x80" * 10)

    def test_protobuf_field_types_and_repeated_fields(self) -> None:
        payload = b"".join((
            proto_varint(1, 25),
            proto_fixed64(2, 14_230_000.0),
            proto_fixed32(3, 2_700.0),
            proto_bytes(4, "MultiV"),
            proto_varint(1, 26),
        ))
        fields = decode_proto_fields(payload)
        self.assertEqual([value for _wire, value in fields[1]], [25, 26])
        self.assertEqual(struct.unpack("<d", fields[2][0][1])[0], 14_230_000.0)
        self.assertEqual(struct.unpack("<f", fields[3][0][1])[0], 2_700.0)
        self.assertEqual(fields[4][0][1], b"MultiV")

    def test_delimited_frame_round_trip(self) -> None:
        left, right = socket.socketpair()
        try:
            left.sendall(encode_delimited_frame(12, b"transmitter-start"))
            member, payload = read_delimited_frame(right)
            self.assertEqual(member, 12)
            self.assertEqual(payload, b"transmitter-start")
        finally:
            left.close()
            right.close()

    def test_client_hello_contains_ticket_and_transmitter_role(self) -> None:
        fields = decode_proto_fields(build_client_hello("stx_gt1_test"))
        self.assertEqual(fields[1][0][1], ROLE_TX_AUDIO)
        self.assertEqual(fields[2][0][1], AUTH_KIND_GAME_TICKET)
        self.assertEqual(fields[3][0][1], b"stx_gt1_test")
        self.assertEqual(fields[4][0][1], b"MultiV")
        self.assertEqual(fields[7][0][1], 1)

    def test_transmitter_start_builds_station_binding(self) -> None:
        start = build_transmitter_start(
            station_id="3f2504e0-4f89-41d3-9a0c-0305e82c3301",
            antenna_id="9b2c1e47-2f0a-4c55-8d1e-6a7b8c9d0e1f",
            modulation="USB",
            frequency_hz=14_230_000.0,
            power_watts=100.0,
            sample_rate=48_000,
        )
        fields = decode_proto_fields(start)
        self.assertEqual(fields[3][0][1], b"USB")
        self.assertEqual(struct.unpack("<d", fields[4][0][1])[0], 14_230_000.0)
        self.assertEqual(fields[7][0][1], 48_000)
        site = decode_proto_fields(fields[1][0][1])
        station_binding = decode_proto_fields(site[1][0][1])
        self.assertEqual(
            station_binding[1][0][1],
            bytes.fromhex("3f2504e04f8941d39a0c0305e82c3301"),
        )
        self.assertEqual(
            station_binding[2][0][1],
            bytes.fromhex("9b2c1e472f0a4c558d1e6a7b8c9d0e1f"),
        )

    def test_audio_sample_block_is_little_endian_float32(self) -> None:
        fields = decode_proto_fields(build_audio_sample_block([0.0, 0.5, -1.0]))
        audio = decode_proto_fields(fields[1][0][1])
        values = struct.unpack("<3f", audio[1][0][1])
        self.assertEqual(values, (0.0, 0.5, -1.0))

    def test_audio_sample_block_rejects_invalid_values(self) -> None:
        with self.assertRaises(ValueError):
            build_audio_sample_block([])
        with self.assertRaises(ValueError):
            build_audio_sample_block([float("nan")])
        with self.assertRaises(ValueError):
            build_audio_sample_block([1.1])


class SimTxAccountClientTests(unittest.TestCase):
    def test_device_authorization_request(self) -> None:
        captured: dict[str, Any] = {}

        def opener(request: Any, timeout: float) -> FakeResponse:
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse({
                "deviceCode": "stx_dc1_secret",
                "userCode": "ABCD-EF23",
                "verificationUri": "https://auth.simtx.net/device",
                "verificationUriComplete": "https://auth.simtx.net/device?code=ABCD-EF23",
                "expiresIn": 600,
                "interval": 5,
            }, status=201)

        client = SimTxAccountClient(opener=opener)
        authorization = client.begin_device_login(scopes=("game.transmit",))
        request = captured["request"]
        self.assertEqual(request.full_url, "https://auth.simtx.net/api/v1/device/authorizations")
        self.assertEqual(request.method, "POST")
        payload = json.loads(request.data)
        self.assertEqual(payload["clientName"], "MultiV")
        self.assertEqual(payload["scope"], "game.transmit")
        self.assertEqual(authorization.user_code, "ABCD-EF23")
        self.assertEqual(authorization.device_code, "stx_dc1_secret")
        self.assertEqual(captured["timeout"], 10.0)

    def test_server_directory_uses_bearer_and_features(self) -> None:
        captured: dict[str, Any] = {}

        def opener(request: Any, timeout: float) -> FakeResponse:
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse({
                "servers": [{
                    "serverId": "argon",
                    "displayName": "Argon",
                    "host": "argon.simtx.net",
                    "port": 5556,
                    "features": ["stations.v1", "pointing"],
                }]
            })

        client = SimTxAccountClient(opener=opener)
        servers = client.list_servers("stx_at1_access")
        request = captured["request"]
        self.assertEqual(request.get_header("Authorization"), "Bearer stx_at1_access")
        self.assertEqual(request.full_url, "https://auth.simtx.net/api/v1/servers?include=features")
        self.assertEqual(servers[0], SimTxServer(
            "argon", "Argon", "argon.simtx.net", 5556, "", ("stations.v1", "pointing")
        ))

    def test_game_ticket_request_uses_station_binding(self) -> None:
        captured: dict[str, Any] = {}

        def opener(request: Any, _timeout: float) -> FakeResponse:
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse({
                "ticket": "stx_gt1_single_use",
                "expiresAt": "2026-11-17T21:20:00Z",
                "station": {
                    "stationId": "3f2504e0-4f89-41d3-9a0c-0305e82c3301",
                    "antennaId": "9b2c1e47-2f0a-4c55-8d1e-6a7b8c9d0e1f",
                },
            }, status=201)

        client = SimTxAccountClient(opener=opener)
        ticket = client.create_game_ticket(
            "stx_at1_access",
            server_id="argon",
            station_id="3f2504e0-4f89-41d3-9a0c-0305e82c3301",
            antenna_id="9b2c1e47-2f0a-4c55-8d1e-6a7b8c9d0e1f",
        )
        request = captured["request"]
        payload = json.loads(request.data)
        self.assertEqual(payload["role"], "tx-audio")
        self.assertEqual(payload["serverId"], "argon")
        self.assertIn("antennaId", payload)
        self.assertEqual(ticket.ticket, "stx_gt1_single_use")
        self.assertEqual(ticket.station["stationId"], payload["stationId"])

    def test_invalid_server_response_is_rejected(self) -> None:
        with self.assertRaises(Exception):
            SimTxServer.from_json({"serverId": "argon", "host": "", "port": 0})

    def test_device_login_wait_handles_pending(self) -> None:
        class StubAccount:
            def __init__(self) -> None:
                self.polls = 0

            def poll_device_token(self, _device_code: str) -> SimTxCredentials:
                self.polls += 1
                if self.polls == 1:
                    raise SimTxApiError(400, "authorization_pending", "Pending")
                return SimTxCredentials("access", "refresh", 600, "game.transmit")

        clock = [0.0]
        account = StubAccount()
        credentials = wait_for_device_login(
            account,  # type: ignore[arg-type]
            SimTxDeviceAuthorization("device", "ABCD-EF23", "uri", "complete", 10, 1),
            sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
            monotonic=lambda: clock[0],
        )
        self.assertEqual(credentials.access_credential, "access")
        self.assertEqual(account.polls, 2)
        self.assertEqual(clock[0], 2.0)


if __name__ == "__main__":
    unittest.main()
