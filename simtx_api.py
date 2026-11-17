"""SimTX account API and protocol-v25 transport primitives for MultiV.

This module is an integration foundation. It handles account-service requests,
device authorization, game tickets, TLS handshakes and the small set of
protobuf messages needed to begin an audio-transmitter session. It does not
own audio capture, pacing or the application UI.
"""
from __future__ import annotations

import json
import math
import socket
import ssl
import struct
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


SIMTX_ACCOUNT_BASE_URL = "https://auth.simtx.net"
SIMTX_PROTOCOL_MAGIC = 0x53494D54
SIMTX_PROTOCOL_VERSION = 25
SIMTX_PROTOCOL_REVISION = 1
SIMTX_DEFAULT_GAME_PORT = 5556
SIMTX_MAX_FRAME_BYTES = 16 * 1024 * 1024
SIMTX_MAX_HELLO_BYTES = 64 * 1024
SIMTX_MAX_AUDIO_SAMPLES = 65_536
SIMTX_CLIENT_NAME = "MultiV"
SIMTX_CLIENT_VERSION = "0.1.0"

ROLE_TX_AUDIO = 2
AUTH_KIND_GAME_TICKET = 1
HELLO_STATUS_ACCEPTED = 1
SESSION_STATE_BOUND = 1

_MAX_HTTP_BODY = 2 * 1024 * 1024


class SimTxError(Exception):
    """Base exception for SimTX integration errors."""


class SimTxApiError(SimTxError):
    """An HTTP/API error returned by the SimTX account service."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        field_errors: Mapping[str, Any] | None = None,
        request_id: str = "",
    ) -> None:
        self.status = int(status)
        self.code = str(code or "request_failed")
        self.message = str(message or "The SimTX request failed.")
        self.field_errors = dict(field_errors or {})
        self.request_id = str(request_id or "")
        details = f"SimTX API error {self.status} ({self.code}): {self.message}"
        if self.request_id:
            details += f" [request {self.request_id}]"
        super().__init__(details)


class SimTxProtocolError(SimTxError):
    """A TLS or game-protocol error."""


class SimTxLoginCancelled(SimTxError):
    """The user cancelled device authorization."""


@dataclass(frozen=True)
class SimTxDeviceAuthorization:
    device_code: str = field(repr=False)
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: int


@dataclass(frozen=True)
class SimTxCredentials:
    access_credential: str = field(repr=False)
    refresh_credential: str = field(repr=False)
    expires_in: int
    scope: str
    credential_type: str = "Bearer"


@dataclass(frozen=True)
class SimTxServer:
    server_id: str
    display_name: str
    host: str
    port: int
    description: str = ""
    features: tuple[str, ...] = ()

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> "SimTxServer":
        try:
            server_id = str(value["serverId"])
            host = str(value["host"])
            port = int(value["port"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SimTxProtocolError("The SimTX server directory returned an invalid entry.") from exc
        if not server_id or not host or not 1 <= port <= 65535:
            raise SimTxProtocolError("The SimTX server directory returned an invalid host or port.")
        features = value.get("features") or ()
        if not isinstance(features, (list, tuple)):
            features = ()
        return cls(
            server_id=server_id,
            display_name=str(value.get("displayName") or server_id),
            host=host,
            port=port,
            description=str(value.get("description") or ""),
            features=tuple(str(item) for item in features),
        )


@dataclass(frozen=True)
class SimTxGameTicket:
    ticket: str = field(repr=False)
    expires_at: str = ""
    station: Mapping[str, Any] | None = None

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> "SimTxGameTicket":
        token = str(value.get("ticket") or "")
        if not token:
            raise SimTxProtocolError("The SimTX ticket response did not include a ticket.")
        station = value.get("station")
        if station is not None and not isinstance(station, Mapping):
            raise SimTxProtocolError("The SimTX ticket response contained an invalid station binding.")
        return cls(
            ticket=token,
            expires_at=str(value.get("expiresAt") or ""),
            station=dict(station) if station is not None else None,
        )


class SimTxAccountClient:
    """Small HTTPS/JSON client for auth.simtx.net.

    The client deliberately keeps credentials in memory. The caller should
    persist refresh credentials with an operating-system credential store.
    """

    def __init__(
        self,
        *,
        base_url: str = SIMTX_ACCOUNT_BASE_URL,
        timeout: float = 10.0,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        base_url = str(base_url).strip().rstrip("/")
        if not base_url.startswith("https://"):
            raise ValueError("The SimTX account API requires HTTPS.")
        self.base_url = base_url
        self.timeout = max(1.0, float(timeout))
        self._opener = opener or urlopen

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
        bearer: str | None = None,
        query: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        if not path.startswith("/"):
            raise ValueError("API paths must start with '/'.")
        url = self.base_url + path
        if query:
            url += "?" + urlencode(query)

        body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "User-Agent": f"{SIMTX_CLIENT_NAME}/{SIMTX_CLIENT_VERSION}",
            "X-Request-ID": str(uuid.uuid4()),
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"

        request = Request(url, data=body, headers=headers, method=method.upper())
        try:
            with self._opener(request, timeout=self.timeout) as response:
                raw = response.read(_MAX_HTTP_BODY + 1)
                status = int(getattr(response, "status", 200))
        except HTTPError as exc:
            raw = exc.read(_MAX_HTTP_BODY + 1)
            self._raise_api_error(exc.code, raw, exc.headers)
        except (URLError, TimeoutError, OSError) as exc:
            raise SimTxError(f"Could not reach the SimTX account service: {exc}") from exc

        if len(raw) > _MAX_HTTP_BODY:
            raise SimTxError("The SimTX account service returned an oversized response.")
        if not raw:
            return {}
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SimTxError("The SimTX account service returned invalid JSON.") from exc
        if not isinstance(result, dict):
            raise SimTxError("The SimTX account service returned an unexpected response.")
        if status < 200 or status >= 300:
            self._raise_api_error(status, raw, getattr(response, "headers", None))
        return result

    @staticmethod
    def _raise_api_error(status: int, raw: bytes, headers: Any = None) -> None:
        try:
            value = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            value = {}
        if not isinstance(value, dict):
            value = {}
        raise SimTxApiError(
            status,
            str(value.get("code") or "request_failed"),
            str(value.get("message") or f"HTTP {status}"),
            field_errors=value.get("fieldErrors") if isinstance(value.get("fieldErrors"), dict) else {},
            request_id=str(value.get("requestId") or (headers.get("X-Request-ID", "") if headers else "")),
        )

    def begin_device_login(
        self,
        *,
        client_name: str = SIMTX_CLIENT_NAME,
        scopes: tuple[str, ...] = ("game.transmit",),
    ) -> SimTxDeviceAuthorization:
        clean_scopes = tuple(dict.fromkeys(str(scope).strip() for scope in scopes if str(scope).strip()))
        result = self._request(
            "POST",
            "/api/v1/device/authorizations",
            payload={
                "clientName": str(client_name).strip()[:80] or SIMTX_CLIENT_NAME,
                "scope": " ".join(clean_scopes),
            },
        )
        try:
            return SimTxDeviceAuthorization(
                device_code=str(result["deviceCode"]),
                user_code=str(result["userCode"]),
                verification_uri=str(result["verificationUri"]),
                verification_uri_complete=str(result.get("verificationUriComplete") or result["verificationUri"]),
                expires_in=max(1, int(result.get("expiresIn", 600))),
                interval=max(1, int(result.get("interval", 5))),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SimTxError("The SimTX device-login response was incomplete.") from exc

    def poll_device_token(self, device_code: str) -> SimTxCredentials:
        result = self._request(
            "POST",
            "/api/v1/device/token",
            payload={"deviceCode": str(device_code)},
        )
        return self._credentials_from_json(result)

    def refresh_credentials(self, refresh_credential: str) -> SimTxCredentials:
        result = self._request(
            "POST",
            "/api/v1/device/refresh",
            payload={"refreshCredential": str(refresh_credential)},
        )
        return self._credentials_from_json(result)

    @staticmethod
    def _credentials_from_json(value: Mapping[str, Any]) -> SimTxCredentials:
        try:
            access = str(value["accessCredential"])
            refresh = str(value["refreshCredential"])
            expires = int(value.get("expiresIn", 600))
        except (KeyError, TypeError, ValueError) as exc:
            raise SimTxError("The SimTX service returned an incomplete credential pair.") from exc
        if not access or not refresh:
            raise SimTxError("The SimTX service returned an empty credential.")
        credential_type = str(value.get("credentialType") or "Bearer")
        if credential_type != "Bearer":
            raise SimTxError(f"Unsupported SimTX credential type: {credential_type}")
        return SimTxCredentials(
            access_credential=access,
            refresh_credential=refresh,
            expires_in=max(1, expires),
            scope=str(value.get("scope") or ""),
            credential_type=credential_type,
        )

    def list_servers(self, access_credential: str) -> list[SimTxServer]:
        result = self._request(
            "GET",
            "/api/v1/servers",
            bearer=access_credential,
            query={"include": "features"},
        )
        entries = result.get("servers")
        if not isinstance(entries, list):
            raise SimTxError("The SimTX server directory response did not contain a server list.")
        return [SimTxServer.from_json(item) for item in entries if isinstance(item, Mapping)]

    def list_stations(self, access_credential: str) -> list[dict[str, Any]]:
        result = self._request("GET", "/api/v1/stations", bearer=access_credential)
        entries = result.get("stations")
        if not isinstance(entries, list):
            raise SimTxError("The SimTX station response did not contain a station list.")
        return [dict(item) for item in entries if isinstance(item, Mapping)]

    def create_game_ticket(
        self,
        access_credential: str,
        *,
        server_id: str,
        role: str = "tx-audio",
        station_id: str,
        antenna_id: str | None = None,
        rig_id: str | None = None,
    ) -> SimTxGameTicket:
        if role not in {"tx-audio", "tx-generated"}:
            raise ValueError("MultiV's initial SimTX transmitter supports tx-audio and tx-generated tickets.")
        if not server_id or not station_id:
            raise ValueError("A server ID and persistent station ID are required.")
        payload: dict[str, Any] = {
            "serverId": str(server_id),
            "role": role,
            "stationId": str(station_id),
        }
        if antenna_id:
            payload["antennaId"] = str(antenna_id)
        if rig_id:
            payload["rigId"] = str(rig_id)
        result = self._request(
            "POST",
            "/api/v1/game-tickets",
            payload=payload,
            bearer=access_credential,
        )
        return SimTxGameTicket.from_json(result)


def wait_for_device_login(
    account: SimTxAccountClient,
    authorization: SimTxDeviceAuthorization,
    *,
    on_wait: Callable[[int], None] | None = None,
    cancel_event: Any = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> SimTxCredentials:
    """Poll until approval, denial, expiry or cancellation.

    The UI should show authorization.user_code and open
    authorization.verification_uri_complete before calling this function.
    """
    deadline = monotonic() + authorization.expires_in
    interval = max(1, authorization.interval)
    while monotonic() < deadline:
        remaining = deadline - monotonic()
        if cancel_event is not None and cancel_event.is_set():
            raise SimTxLoginCancelled("SimTX sign-in was cancelled.")
        delay = min(interval, max(0.0, remaining))
        if on_wait:
            on_wait(interval)
        if cancel_event is not None and hasattr(cancel_event, "wait"):
            if cancel_event.wait(delay):
                raise SimTxLoginCancelled("SimTX sign-in was cancelled.")
        else:
            sleep(delay)
        if monotonic() >= deadline:
            break
        try:
            return account.poll_device_token(authorization.device_code)
        except SimTxApiError as exc:
            if exc.code == "authorization_pending":
                continue
            if exc.code == "slow_down":
                try:
                    interval = max(interval + 5, int(exc.field_errors.get("interval", interval + 5)))
                except (TypeError, ValueError):
                    interval += 5
                continue
            if exc.code == "expired_token":
                raise SimTxApiError(400, "expired_token", "The SimTX sign-in code expired. Start again.") from exc
            if exc.code == "access_denied":
                raise SimTxApiError(400, "access_denied", "SimTX sign-in was denied.") from exc
            raise
    raise SimTxApiError(400, "expired_token", "The SimTX sign-in code expired. Start again.")


class SimTxCredentialManager:
    """Serializes refresh-token rotation and keeps access credentials in memory."""

    def __init__(
        self,
        account: SimTxAccountClient,
        credentials: SimTxCredentials,
        *,
        persist_refresh: Callable[[str], None] | None = None,
    ) -> None:
        import threading

        self.account = account
        self._credentials = credentials
        self._expires_at = time.monotonic() + credentials.expires_in
        self._persist_refresh = persist_refresh
        self._lock = threading.RLock()

    def access_credential(self, *, refresh_margin: float = 30.0) -> str:
        with self._lock:
            if time.monotonic() < self._expires_at - max(0.0, refresh_margin):
                return self._credentials.access_credential
            rotated = self.account.refresh_credentials(self._credentials.refresh_credential)
            if self._persist_refresh is not None:
                self._persist_refresh(rotated.refresh_credential)
            self._credentials = rotated
            self._expires_at = time.monotonic() + rotated.expires_in
            return rotated.access_credential

    def replace_credentials(self, credentials: SimTxCredentials) -> None:
        with self._lock:
            if self._persist_refresh is not None:
                self._persist_refresh(credentials.refresh_credential)
            self._credentials = credentials
            self._expires_at = time.monotonic() + credentials.expires_in

    def clear(self, *, delete_refresh: Callable[[], None] | None = None) -> None:
        with self._lock:
            if delete_refresh is not None:
                delete_refresh()
            self._credentials = SimTxCredentials("", "", 1, "")
            self._expires_at = 0.0


# Minimal protobuf wire helpers. These cover the fields used by the v25
# account handshake and audio-transmitter start/sample messages.
def encode_varint(value: int) -> bytes:
    value = int(value)
    if value < 0 or value > 0xFFFFFFFFFFFFFFFF:
        raise ValueError("A protobuf varint must be an unsigned 64-bit value.")
    output = bytearray()
    while value > 0x7F:
        output.append((value & 0x7F) | 0x80)
        value >>= 7
    output.append(value)
    return bytes(output)


def decode_varint(data: bytes, offset: int = 0) -> tuple[int, int]:
    value = 0
    shift = 0
    for _ in range(10):
        if offset >= len(data):
            raise ValueError("Truncated protobuf varint.")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            if value > 0xFFFFFFFFFFFFFFFF:
                raise ValueError("Protobuf varint exceeds 64 bits.")
            return value, offset
        shift += 7
    raise ValueError("Malformed protobuf varint.")


def proto_varint(field_number: int, value: int) -> bytes:
    if field_number < 1:
        raise ValueError("Protobuf field numbers start at 1.")
    return encode_varint((field_number << 3) | 0) + encode_varint(value)


def proto_fixed64(field_number: int, value: float) -> bytes:
    if field_number < 1:
        raise ValueError("Protobuf field numbers start at 1.")
    return encode_varint((field_number << 3) | 1) + struct.pack("<d", float(value))


def proto_fixed32(field_number: int, value: float) -> bytes:
    if field_number < 1:
        raise ValueError("Protobuf field numbers start at 1.")
    return encode_varint((field_number << 3) | 5) + struct.pack("<f", float(value))


def proto_bytes(field_number: int, value: bytes | bytearray | memoryview | str) -> bytes:
    if field_number < 1:
        raise ValueError("Protobuf field numbers start at 1.")
    if isinstance(value, str):
        raw = value.encode("utf-8")
    else:
        raw = bytes(value)
    return encode_varint((field_number << 3) | 2) + encode_varint(len(raw)) + raw


def decode_proto_fields(data: bytes) -> dict[int, list[tuple[int, int | bytes]]]:
    """Decode protobuf wire types 0, 1, 2 and 5; preserve repeated fields."""
    fields: dict[int, list[tuple[int, int | bytes]]] = {}
    offset = 0
    while offset < len(data):
        key, offset = decode_varint(data, offset)
        number, wire_type = key >> 3, key & 7
        if number < 1:
            raise ValueError("Invalid protobuf field number.")
        if wire_type == 0:
            value, offset = decode_varint(data, offset)
        elif wire_type == 1:
            end = offset + 8
            if end > len(data):
                raise ValueError("Truncated protobuf fixed64 field.")
            value = data[offset:end]
            offset = end
        elif wire_type == 2:
            length, offset = decode_varint(data, offset)
            end = offset + length
            if end > len(data):
                raise ValueError("Truncated protobuf bytes field.")
            value = data[offset:end]
            offset = end
        elif wire_type == 5:
            end = offset + 4
            if end > len(data):
                raise ValueError("Truncated protobuf fixed32 field.")
            value = data[offset:end]
            offset = end
        else:
            raise ValueError(f"Unsupported protobuf wire type {wire_type}.")
        fields.setdefault(number, []).append((wire_type, value))
    return fields


def _first(fields: Mapping[int, list[tuple[int, int | bytes]]], number: int, default: Any = None) -> Any:
    values = fields.get(number)
    return values[0][1] if values else default


def _first_text(fields: Mapping[int, list[tuple[int, int | bytes]]], number: int) -> str:
    value = _first(fields, number, b"")
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


def encode_delimited_frame(member_number: int, payload: bytes) -> bytes:
    """Wrap a message in ClientFrame/ServerFrame oneof framing."""
    frame_body = proto_bytes(member_number, payload)
    if len(frame_body) > SIMTX_MAX_FRAME_BYTES:
        raise ValueError("The SimTX frame exceeds the 16 MiB protocol limit.")
    return encode_varint(len(frame_body)) + frame_body


def _read_exact(sock: socket.socket, length: int) -> bytes:
    output = bytearray()
    while len(output) < length:
        chunk = sock.recv(length - len(output))
        if not chunk:
            raise ConnectionError("The SimTX game server closed the connection.")
        output.extend(chunk)
    return bytes(output)


def read_delimited_frame(
    sock: socket.socket,
    *,
    max_frame_bytes: int = SIMTX_MAX_FRAME_BYTES,
) -> tuple[int, bytes]:
    """Read one varint-length-delimited ClientFrame/ServerFrame."""
    prefix = bytearray()
    for _ in range(5):
        byte = _read_exact(sock, 1)[0]
        prefix.append(byte)
        if not byte & 0x80:
            break
    else:
        raise SimTxProtocolError("The SimTX frame length prefix is malformed.")
    try:
        length, offset = decode_varint(bytes(prefix))
    except ValueError as exc:
        raise SimTxProtocolError(f"Invalid SimTX frame length: {exc}") from exc
    if offset != len(prefix) or length <= 0 or length > max_frame_bytes:
        raise SimTxProtocolError(f"The SimTX frame length {length} is outside the allowed range.")
    fields = decode_proto_fields(_read_exact(sock, length))
    members = [(number, values[0][1]) for number, values in fields.items() if values]
    if not members:
        return 0, b""
    if len(members) != 1:
        raise SimTxProtocolError("A SimTX frame must contain exactly one message.")
    member, payload = members[0]
    if not isinstance(payload, bytes):
        raise SimTxProtocolError("The SimTX frame message did not use a protobuf message field.")
    return member, payload


def build_client_hello(
    game_ticket: str,
    *,
    client_name: str = SIMTX_CLIENT_NAME,
    client_version: str = SIMTX_CLIENT_VERSION,
) -> bytes:
    if not str(game_ticket).strip():
        raise ValueError("A single-use SimTX game ticket is required.")
    return b"".join((
        proto_varint(1, ROLE_TX_AUDIO),
        proto_varint(2, AUTH_KIND_GAME_TICKET),
        proto_bytes(3, str(game_ticket)),
        proto_bytes(4, str(client_name)[:80]),
        proto_bytes(5, str(client_version)[:40]),
        proto_varint(7, SIMTX_PROTOCOL_REVISION),
    ))


def _uuid_bytes(value: str, label: str, *, required: bool) -> bytes:
    if not value:
        if required:
            raise ValueError(f"A SimTX {label} is required.")
        return b""
    try:
        return uuid.UUID(str(value)).bytes
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"The SimTX {label} must be a UUID.") from exc


def build_transmitter_start(
    *,
    station_id: str,
    antenna_id: str = "",
    rig_id: str = "",
    modulation: str = "USB",
    frequency_hz: float,
    power_watts: float,
    sample_rate: int = 48_000,
    audio_gain: float = 1.0,
    bandwidth_hz: float = 2_700.0,
    audio_low_cut_hz: float = 300.0,
    duty_cycle: float = 1.0,
    source_type: str = "AUDIO_STREAM",
) -> bytes:
    """Build the v25 station-bound TransmitterStart message."""
    if not math.isfinite(frequency_hz) or frequency_hz <= 0:
        raise ValueError("Frequency must be a positive finite value in hertz.")
    if not math.isfinite(power_watts) or power_watts < 0:
        raise ValueError("Power must be a finite non-negative value in watts.")
    if not 8_000 <= int(sample_rate) <= 10_000_000:
        raise ValueError("The SimTX sample rate must be between 8 kHz and 10 MHz.")
    for label, value in (
        ("audio gain", audio_gain),
        ("bandwidth", bandwidth_hz),
        ("audio low cut", audio_low_cut_hz),
        ("duty cycle", duty_cycle),
    ):
        if not math.isfinite(float(value)) or float(value) < 0:
            raise ValueError(f"SimTX {label} must be finite and non-negative.")
    station_binding = b"".join((
        proto_bytes(1, _uuid_bytes(station_id, "station ID", required=True)),
        proto_bytes(2, _uuid_bytes(antenna_id, "antenna ID", required=False)),
        proto_bytes(3, _uuid_bytes(rig_id, "rig ID", required=False)),
    ))
    site = proto_bytes(1, station_binding)  # SessionSite.station
    return b"".join((
        proto_bytes(1, site),
        proto_bytes(3, str(modulation)),
        proto_fixed64(4, frequency_hz),
        proto_fixed32(5, power_watts),
        proto_bytes(6, str(source_type)),
        proto_varint(7, int(sample_rate)),
        proto_fixed32(8, audio_gain),
        proto_fixed32(9, bandwidth_hz),
        proto_fixed32(10, audio_low_cut_hz),
        proto_fixed32(12, duty_cycle),
    ))


def build_audio_sample_block(samples: Any) -> bytes:
    """Build SampleBlock.audio.values from normalized mono Float32 samples."""
    values = [float(sample) for sample in samples]
    if not values:
        raise ValueError("An audio sample block cannot be empty.")
    if len(values) > SIMTX_MAX_AUDIO_SAMPLES:
        raise ValueError(f"A SimTX sample block cannot exceed {SIMTX_MAX_AUDIO_SAMPLES} samples.")
    if any(not math.isfinite(value) or value < -1.0 or value > 1.0 for value in values):
        raise ValueError("SimTX audio samples must be finite and within -1.0 to +1.0.")
    packed = struct.pack(f"<{len(values)}f", *values)
    audio = proto_bytes(1, packed)       # PackedFloats.values
    return proto_bytes(1, audio)         # SampleBlock.audio


@dataclass(frozen=True)
class SimTxFrame:
    member_number: int
    payload: bytes


class SimTxGameConnection:
    """TLS connection wrapper for a single SimTX game-protocol session.

    A connection is one session. A future MultiV transmitter worker should
    own this object, pace SampleBlock writes at the negotiated sample rate,
    and keep reading telemetry for the lifetime of the connection.
    """

    def __init__(self, sock: ssl.SSLSocket, server: SimTxServer) -> None:
        self.socket = sock
        self.server = server
        self._closed = False
        self._tx_started = False
        self._tx_accepted = False
        self.server_hello: dict[int, list[tuple[int, int | bytes]]] = {}

    @classmethod
    def connect(
        cls,
        server: SimTxServer,
        game_ticket: str,
        *,
        client_name: str = SIMTX_CLIENT_NAME,
        client_version: str = SIMTX_CLIENT_VERSION,
        timeout: float = 10.0,
    ) -> "SimTxGameConnection":
        if not isinstance(server, SimTxServer):
            raise TypeError("A SimTxServer from the server directory is required.")
        raw = socket.create_connection((server.host, server.port), timeout=timeout)
        try:
            raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            raw.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            tls_context = ssl.create_default_context()
            sock = tls_context.wrap_socket(raw, server_hostname=server.host)
            sock.settimeout(timeout)
            sock.sendall(struct.pack(">II", SIMTX_PROTOCOL_MAGIC, SIMTX_PROTOCOL_VERSION))
            sock.sendall(encode_delimited_frame(
                1,
                build_client_hello(
                    game_ticket,
                    client_name=client_name,
                    client_version=client_version,
                ),
            ))
            member, payload = read_delimited_frame(sock, max_frame_bytes=SIMTX_MAX_HELLO_BYTES)
            if member != 1:
                raise SimTxProtocolError(f"Expected SimTX ServerHello, received frame {member}.")
            hello = decode_proto_fields(payload)
            status = int(_first(hello, 1, 0))
            if status != HELLO_STATUS_ACCEPTED:
                detail = _first_text(hello, 2)
                raise SimTxProtocolError(
                    f"SimTX rejected the game connection (hello status {status}): {detail or 'No reason supplied.'}"
                )
            connection = cls(sock, server)
            connection.server_hello = hello
            return connection
        except Exception:
            try:
                raw.close()
            except OSError:
                pass
            raise

    def send_transmitter_start(self, **settings: Any) -> None:
        if self._closed:
            raise RuntimeError("The SimTX connection is closed.")
        if self._tx_started:
            raise RuntimeError("The SimTX transmitter session has already been started.")
        payload = build_transmitter_start(**settings)
        self.socket.sendall(encode_delimited_frame(12, payload))
        self._tx_started = True

    def read_frame(self, *, timeout: float | None = None) -> SimTxFrame:
        if self._closed:
            raise RuntimeError("The SimTX connection is closed.")
        previous_timeout = self.socket.gettimeout()
        if timeout is not None:
            self.socket.settimeout(max(0.1, float(timeout)))
        try:
            member, payload = read_delimited_frame(self.socket)
            return SimTxFrame(member, payload)
        finally:
            if timeout is not None and not self._closed:
                self.socket.settimeout(previous_timeout)

    def wait_for_transmitter_accept(self, *, timeout: float = 60.0) -> None:
        if not self._tx_started:
            raise RuntimeError("Send TransmitterStart before waiting for admission.")
        deadline = time.monotonic() + max(0.1, float(timeout))
        while time.monotonic() < deadline:
            try:
                frame = self.read_frame(timeout=min(5.0, deadline - time.monotonic()))
            except socket.timeout:
                continue
            if frame.member_number == 14:  # TxAccept
                self._tx_accepted = True
                return
            if frame.member_number == 10:  # SessionStatus
                status = decode_proto_fields(frame.payload)
                state = int(_first(status, 1, 0))
                if state != SESSION_STATE_BOUND:
                    code = int(_first(status, 3, 0))
                    reason = _first_text(status, 2)
                    raise SimTxProtocolError(
                        f"SimTX refused the transmitter session (state {state}, code {code}): "
                        f"{reason or 'No reason supplied.'}"
                    )
            elif frame.member_number == 3:  # Error
                error = decode_proto_fields(frame.payload)
                code = int(_first(error, 1, 0))
                reason = _first_text(error, 2)
                raise SimTxProtocolError(f"SimTX game error {code}: {reason}")
            # TxQueue and unrelated status frames are consumed while waiting.
        raise TimeoutError("SimTX did not admit the transmitter before the timeout.")

    def send_audio_samples(self, samples: Any) -> None:
        if self._closed:
            raise RuntimeError("The SimTX connection is closed.")
        if not self._tx_accepted:
            raise RuntimeError("SimTX has not accepted the transmitter session yet.")
        self.socket.sendall(encode_delimited_frame(13, build_audio_sample_block(samples)))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.socket.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        try:
            self.socket.close()
        except OSError:
            pass

    def __enter__(self) -> "SimTxGameConnection":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.close()
