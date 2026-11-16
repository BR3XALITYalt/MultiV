"""TXITPY desktop client for TXITY's station directory, status feed, and virtual transmitter.

Discord sign-in uses TXITY's official OAuth route and a one-use loopback callback.
"""
from __future__ import annotations

import json
import queue
import re
import secrets
import sys
import threading
import uuid
import webbrowser
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

from discord_auth import (
    DiscordAuthManager,
    clean_discord_user,
    format_exception_details,
    is_auth_token,
    safe_error_text,
    write_diagnostic,
    write_diagnostic_text,
)
from transmitter import (
    TxityTransmitter,
    build_transmit_config,
    find_frequency_overlaps,
    list_audio_inputs,
    list_loopback_devices,
    parse_rf_frequency,
)

BASE_URL = "https://txity.radiodiscussion.org"
CONTROL_URL = "wss://txity.radiodiscussion.org/ws/control"
USER_AGENT = "TXITPY/0.1 (+https://github.com/BR3XALITYalt/TXITPY)"
HTTP_TIMEOUT = 12
MAX_STATION_IMAGE_BYTES = 5 * 1024 * 1024


@dataclass(slots=True)
class Station:
    station_id: str
    name: str = "Unknown station"
    description: str = ""
    description_source: str = ""
    frequency: str = ""
    frequency_hz: int | None = None
    allow_overlap_visitors: bool | None = None
    rating: str = ""
    mode: str = ""
    details: str = ""
    operator: str = ""
    on_air: str = ""
    logo_url: str = ""
    profile_details: str = ""
    discord_id: str = ""
    avatar_url: str = ""
    image_urls: list[str] | None = None
    page_layout: dict[str, Any] | None = None
    raw_page_layout: str = ""
    likes: int = 0
    rating_average: float | None = None
    rating_count: int = 0
    comments: list[dict[str, str]] | None = None
    comment_count: int = 0

    def as_row(self) -> dict[str, Any]:
        return {
            "id": self.station_id,
            "station": self.name,
            "operator": self.operator,
            "frequency": self.frequency,
            "frequency_hz": self.frequency_hz,
            "allow_overlap_visitors": self.allow_overlap_visitors,
            "mode": self.mode,
            "on_air": self.on_air,
            "rating": self.rating,
            "description": self.description,
            "description_source": self.description_source,
        }

    def as_record(self) -> dict[str, Any]:
        return {
            **self.as_row(),
            "logo_url": self.logo_url,
            "details_text": self.profile_details,
            "discord_id": self.discord_id,
            "avatar_url": self.avatar_url,
            "image_urls": self.image_urls or [],
            "page_layout": self.page_layout,
            "page_layout_json": self.raw_page_layout,
            "likes": self.likes,
            "rating_average": self.rating_average,
            "rating_count": self.rating_count,
            "comments": self.comments or [],
            "comment_count": self.comment_count,
        }


class StationPageParser(HTMLParser):
    """Extract station cards from TXITY's public server-rendered HTML."""

    VOID_TAGS = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stations: list[Station] = []
        self.stack: list[tuple[str, set[str]]] = []
        self.current: Station | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if tag == "button" and "station-row" in classes:
            self._finish_current()
            self.current = Station(
                station_id=attributes.get("data-session-id") or str(uuid.uuid4())
            )
        if tag not in self.VOID_TAGS:
            self.stack.append((tag, classes))

    def handle_endtag(self, tag: str) -> None:
        if self.current is not None and tag == "button":
            self._finish_current()
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if self.current is None:
            return
        value = " ".join(data.split())
        if not value:
            return

        classes = set().union(*(entry[1] for entry in self.stack)) if self.stack else set()
        tag = self.stack[-1][0] if self.stack else ""

        if tag == "strong" and "station-card-main" in classes and self.current.name == "Unknown station":
            self.current.name = value
        elif "station-frequency" in classes:
            self.current.frequency = self._append(self.current.frequency, value)
        elif "station-rating-badge" in classes:
            self.current.rating = self._append(self.current.rating, value)
        elif "station-card-meta" in classes:
            self.current.details = self._append(self.current.details, value)
        elif tag == "p" and "station-card-main" in classes and "station-owner-line" not in classes:
            self.current.description = self._append(self.current.description, value)

    @staticmethod
    def _append(previous: str, value: str) -> str:
        return f"{previous} {value}".strip()

    def _finish_current(self) -> None:
        if self.current is None:
            return
        station = self.current
        self.current = None
        station.mode = station.details.split("·", 1)[0].strip() if station.details else ""
        if station.name != "Unknown station" or station.frequency:
            self.stations.append(station)


def parse_stations(html: str) -> list[Station]:
    parser = StationPageParser()
    parser.feed(html)
    parser.close()
    parser._finish_current()
    return parser.stations


def format_frequency(value: Any) -> str:
    try:
        hz = float(value)
    except (TypeError, ValueError):
        return "Not reported"
    if hz >= 1_000_000:
        return f"{hz / 1_000_000:.3f} MHz"
    if hz >= 1_000:
        return f"{hz / 1_000:.1f} kHz"
    return f"{hz:.0f} Hz"


def fetch_text(url: str) -> str:
    request = Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/json"},
    )
    with urlopen(request, timeout=HTTP_TIMEOUT) as response:
        encoding = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(encoding, errors="replace")


def format_duration(value: Any) -> str:
    """Format an on-air duration supplied by TXITY in seconds."""
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        return "Not reported"
    total_seconds = int(value)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def describe_media_url(value: str) -> str:
    """Return a console-safe media URL without query strings or fragments."""
    try:
        parsed = urlparse(value)
        if not parsed.scheme or not parsed.netloc:
            return value[:180]
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"[:240]
    except ValueError:
        return "[invalid media URL]"


def normalize_media_url(value: Any) -> str:
    """Allow station media only from TXITY or Discord's image CDN."""
    if isinstance(value, dict):
        value = next((
            value.get(key) for key in ("url", "src", "imageUrl", "image_url", "href")
            if isinstance(value.get(key), str) and value.get(key).strip()
        ), "")
    if not isinstance(value, str) or not value.strip():
        return ""
    raw = value.strip()
    if raw.startswith("//"):
        raw = f"https:{raw}"
    url = urljoin(f"{BASE_URL}/", raw)
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {
        "txity.radiodiscussion.org",
        "cdn.discordapp.com",
        "media.discordapp.net",
    }:
        print(
            f"[TXITPY] Media URL rejected: {describe_media_url(url)} "
            f"(unsupported scheme or host: {parsed.scheme}://{parsed.hostname or 'unknown'}).",
            file=sys.stderr,
            flush=True,
        )
        return ""
    return url


def discord_avatar_url(avatar_value: Any, user_id: Any = None) -> str:
    """Resolve either a Discord avatar URL or a raw avatar hash."""
    if isinstance(avatar_value, dict):
        avatar_value = next((
            avatar_value.get(key) for key in ("url", "src", "avatarUrl", "avatar_url")
            if isinstance(avatar_value.get(key), str) and avatar_value.get(key).strip()
        ), "")

    avatar_hash = avatar_value.strip() if isinstance(avatar_value, str) else ""
    user_id_text = str(user_id).strip() if isinstance(user_id, (str, int)) and not isinstance(user_id, bool) else ""

    # A Discord avatar hash is not a URL. Resolve it before relative URL joining,
    # otherwise it would incorrectly become a path on the TXITY website.
    if (
        re.fullmatch(r"[A-Za-z0-9_]{8,128}", avatar_hash)
        and re.fullmatch(r"\d{5,24}", user_id_text)
        and not avatar_hash.startswith(("/", "http:", "https:"))
    ):
        extension = "gif" if avatar_hash.startswith("a_") else "png"
        return f"https://cdn.discordapp.com/avatars/{user_id_text}/{avatar_hash}.{extension}?size=128"

    return normalize_media_url(avatar_value)


def parse_station_page_layout(value: Any) -> dict[str, Any] | None:
    """Keep only a small, display-safe subset of a station's custom page layout."""
    if isinstance(value, str):
        if len(value) > 40_000:
            return None
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if not isinstance(value, dict):
        return None

    raw_blocks = value.get("blocks")
    if not isinstance(raw_blocks, list):
        raw_blocks = value.get("elements")
    if not isinstance(raw_blocks, list):
        return None

    blocks: list[dict[str, Any]] = []
    for item in raw_blocks[:24]:
        if not isinstance(item, dict):
            continue
        block_type = item.get("type", item.get("kind", "text"))
        if not isinstance(block_type, str):
            block_type = "text"
        block_type = block_type.casefold().replace("layout-", "")
        if block_type not in {"text", "image", "separator"}:
            continue

        text_value = item.get("text", item.get("content", ""))
        if not isinstance(text_value, str):
            text_value = ""
        image_url = normalize_media_url(
            item.get("imageUrl", item.get("src", item.get("url", "")))
        )

        def number(*names: str, default: float) -> float:
            for name in names:
                candidate = item.get(name)
                if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
                    return float(candidate)
            return default

        blocks.append({
            "type": block_type,
            "text": text_value[:2_000],
            "image_url": image_url,
            "x": max(0.0, min(100.0, number("x", "left", default=8.0))),
            "y": max(0.0, min(100.0, number("y", "top", default=8.0))),
            "width": max(5.0, min(100.0, number("width", "w", default=84.0))),
            "height": max(8.0, min(900.0, number("height", "h", default=72.0))),
            "font": item.get("fontFamily", item.get("font", "Segoe UI"))
                if isinstance(item.get("fontFamily", item.get("font", "Segoe UI")), str) else "Segoe UI",
            "font_size": max(10, min(72, int(number("fontSize", "font_size", default=18)))),
            "font_weight": max(300, min(900, int(number("fontWeight", "font_weight", default=400)))),
            "align": item.get("textAlign", item.get("align", "left"))
                if item.get("textAlign", item.get("align", "left")) in {"left", "center", "right"} else "left",
        })
    if not blocks:
        return None

    height = value.get("height", value.get("pageHeight", 420))
    if not isinstance(height, (int, float)) or isinstance(height, bool):
        height = 420
    return {"height": max(320, min(1_200, int(height))), "blocks": blocks}


def _clean_station_comments(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    comments: list[dict[str, str]] = []

    def first_text(source: Any, *names: str, limit: int = 600) -> str:
        if not isinstance(source, dict):
            return ""
        for name in names:
            candidate = source.get(name)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()[:limit]
        return ""

    for item in value[:30]:
        if not isinstance(item, dict):
            continue

        user = {}
        for nested_key in ("user", "author", "discordUser", "discord_user"):
            candidate_user = item.get(nested_key)
            if isinstance(candidate_user, dict) and candidate_user:
                user = candidate_user
                break

        author = (
            first_text(item, "discordUsername", "username", "authorName", "name", limit=80)
            or first_text(user, "globalName", "global_name", "username", "name", limit=80)
            or "TXITY user"
        )
        text_value = first_text(item, "text", "content", "body", "message")
        if not text_value:
            continue

        avatar_value: Any = ""
        avatar_fields = (
            "discordAvatarUrl",
            "avatarUrl",
            "avatar_url",
            "authorAvatarUrl",
            "avatar",
            "discordAvatar",
            "discord_avatar",
        )
        for source in (item, user):
            for field_name in avatar_fields:
                candidate_avatar = source.get(field_name) if isinstance(source, dict) else None
                if (
                    (isinstance(candidate_avatar, str) and candidate_avatar.strip())
                    or isinstance(candidate_avatar, dict)
                ):
                    avatar_value = candidate_avatar
                    break
            if avatar_value:
                break

        avatar_user_id = ""
        for source in (item, user):
            for field_name in ("discordId", "userId", "authorId", "id"):
                candidate_id = source.get(field_name) if isinstance(source, dict) else None
                if (
                    isinstance(candidate_id, (str, int))
                    and not isinstance(candidate_id, bool)
                    and str(candidate_id).isdigit()
                ):
                    avatar_user_id = str(candidate_id)
                    break
            if avatar_user_id:
                break
        avatar_url = discord_avatar_url(avatar_value, avatar_user_id)
        comments.append({
            "author": author,
            "text": text_value,
            "timestamp": (
                first_text(item, "createdAtLocal", "createdAt", "timestamp", "date", limit=80)
                or first_text(user, "createdAtLocal", "createdAt", limit=80)
            ),
            "avatar_url": avatar_url,
        })
    return comments


def parse_users_listing(payload: Any) -> tuple[dict[str, Any], list[Station]]:
    """Parse TXITY's public listing and bounded station-page metadata."""
    if not isinstance(payload, dict):
        raise ValueError("TXITY users.json did not return a JSON object.")

    raw_stations = payload.get("stations")
    if not isinstance(raw_stations, list):
        raise ValueError("TXITY users.json did not contain a stations array.")

    metric_names = (
        "webClients",
        "webAudioClients",
        "sdrppClients",
        "sdrppStreamingClients",
        "sessions",
        "transmitting",
    )
    metrics: dict[str, Any] = {
        name: payload.get(name)
        for name in metric_names
        if isinstance(payload.get(name), int)
        and not isinstance(payload.get(name), bool)
        and payload.get(name) >= 0
    }
    updated_at = payload.get("updatedAtLocal")
    if isinstance(updated_at, str) and updated_at.strip():
        metrics["updatedAtLocal"] = updated_at.strip()[:64]

    stations: list[Station] = []
    for index, item in enumerate(raw_stations):
        if not isinstance(item, dict):
            continue

        station_id = item.get("sessionId")
        if not isinstance(station_id, str) or not station_id.strip():
            station_id = f"station-{index}"

        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            name = "Unknown station"

        description = ""
        description_source = ""
        for field_name in ("shortDescription", "short_description", "description", "summary"):
            candidate = item.get(field_name)
            if isinstance(candidate, str) and candidate.strip():
                description = candidate.strip()
                description_source = field_name
                break

        raw_details = item.get("details")
        if isinstance(raw_details, str):
            profile_details = raw_details.strip()
        elif isinstance(raw_details, dict):
            profile_details = ""
            for field_name in (
                "text", "description", "shortDescription", "summary",
                "content", "body", "about",
            ):
                candidate = raw_details.get(field_name)
                if isinstance(candidate, str) and candidate.strip():
                    profile_details = candidate.strip()
                    break
        else:
            profile_details = ""

        page_layout_value = item.get("pageLayout")
        page_layout = parse_station_page_layout(page_layout_value)
        if isinstance(page_layout_value, str):
            raw_page_layout = page_layout_value[:40_000]
        elif isinstance(page_layout_value, dict):
            try:
                encoded_layout = json.dumps(page_layout_value, separators=(",", ":"), ensure_ascii=False)
                raw_page_layout = encoded_layout if len(encoded_layout) <= 40_000 else ""
            except (TypeError, ValueError):
                raw_page_layout = ""
        else:
            raw_page_layout = ""

        if not description and profile_details:
            description = profile_details
            description_source = "details"

        if not description and page_layout:
            for block in page_layout.get("blocks", []):
                candidate = block.get("text")
                if block.get("type") == "text" and isinstance(candidate, str) and candidate.strip():
                    description = candidate.strip()
                    description_source = "pageLayout"
                    break

        raw_frequency = item.get("frequency")
        if not isinstance(raw_frequency, (str, int, float)) or not str(raw_frequency).strip():
            raw_frequency = item.get("frequencyHz", item.get("rfHz"))
        frequency_hz = None
        for candidate in (item.get("frequencyHz"), item.get("rfHz"), item.get("frequency_hz")):
            if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
                if candidate > 0:
                    frequency_hz = int(round(candidate))
                    break
        if frequency_hz is None:
            try:
                frequency_hz = parse_rf_frequency(raw_frequency)
            except (TypeError, ValueError):
                frequency_hz = None
        if isinstance(raw_frequency, str) and raw_frequency.strip():
            frequency = raw_frequency.strip()
        elif frequency_hz is not None:
            frequency = format_frequency(frequency_hz)
        else:
            frequency = "Not reported"
        overlap_setting = item.get("allowOverlapVisitors")
        allow_overlap_visitors = overlap_setting if isinstance(overlap_setting, bool) else None

        mode = item.get("mode")
        if not isinstance(mode, str):
            mode = ""

        average = item.get("ratingAverage")
        count = item.get("ratingCount")
        average_value = (
            float(average)
            if isinstance(average, (int, float)) and not isinstance(average, bool)
            else None
        )
        rating_count = count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else 0
        rating = f"★ {average_value:.2f} ({rating_count})" if average_value is not None and rating_count else "Not rated"

        operator = item.get("discordUsername")
        if not isinstance(operator, str):
            operator = ""

        raw_images = item.get("imageUrls")
        image_urls = [
            normalized for normalized in (
                normalize_media_url(value) for value in raw_images[:6]
            ) if normalized
        ] if isinstance(raw_images, list) else []
        logo_url = normalize_media_url(item.get("logoUrl")) or (image_urls[0] if image_urls else "")

        comments = _clean_station_comments(item.get("comments"))
        likes = item.get("likes")
        comment_count = item.get("commentCount")
        stations.append(Station(
            station_id=station_id.strip()[:160],
            name=name.strip()[:120],
            description=description.strip()[:1_000],
            description_source=description_source,
            frequency=frequency.strip()[:40],
            frequency_hz=frequency_hz,
            allow_overlap_visitors=allow_overlap_visitors,
            rating=rating,
            mode=mode.strip()[:24],
            operator=operator.strip()[:100],
            on_air=format_duration(item.get("onAirSeconds")),
            logo_url=logo_url,
            profile_details=profile_details.strip()[:4_000],
            discord_id=str(item.get("discordId", ""))[:40] if item.get("discordId") is not None else "",
            avatar_url=discord_avatar_url(
                item.get("discordAvatarUrl")
                or item.get("avatarUrl")
                or item.get("discordAvatar")
                or item.get("avatar"),
                item.get("discordId") or item.get("userId"),
            ),
            image_urls=image_urls,
            page_layout=page_layout,
            raw_page_layout=raw_page_layout,
            likes=likes if isinstance(likes, int) and not isinstance(likes, bool) and likes >= 0 else 0,
            rating_average=average_value,
            rating_count=rating_count,
            comments=comments,
            comment_count=comment_count if isinstance(comment_count, int) and not isinstance(comment_count, bool) and comment_count >= 0 else len(comments),
        ))

    return metrics, stations


def fetch_station_image(url: str) -> bytes:
    """Fetch and validate a bounded image from an approved media host."""
    safe_url = normalize_media_url(url)
    if not safe_url:
        raise ValueError("Station image URL is not from an approved host.")

    request = Request(
        safe_url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "Referer": f"{BASE_URL}/",
        },
    )
    with urlopen(request, timeout=HTTP_TIMEOUT) as response:
        final_url = normalize_media_url(response.geturl())
        if not final_url:
            raise ValueError("Station image redirected to an unsupported host.")

        status = getattr(response, "status", None) or response.getcode()
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        data = response.read(MAX_STATION_IMAGE_BYTES + 1)
        if len(data) > MAX_STATION_IMAGE_BYTES:
            raise ValueError(
                f"HTTP {status}: image response exceeds the 5 MB limit "
                f"(Content-Type: {content_type or 'missing'})."
            )

        if content_type.startswith("image/"):
            return data

        # Some upload servers send a generic MIME type for real images.
        # Verify the bytes instead of rejecting a valid image based only on its header.
        try:
            from io import BytesIO
            from PIL import Image

            with Image.open(BytesIO(data)) as image:
                image_format = image.format or "unknown"
                image.verify()
        except Exception as exc:
            raise ValueError(
                f"HTTP {status}: response was not a decodable image "
                f"(Content-Type: {content_type or 'missing'}; "
                f"URL: {describe_media_url(final_url)})."
            ) from exc

        print(
            f"[TXITPY] Image MIME mismatch accepted: "
            f"Content-Type={content_type or 'missing'}, detected={image_format} "
            f"({describe_media_url(final_url)}).",
            file=sys.stderr,
            flush=True,
        )
        return data


def describe_status_socket_error(error: BaseException) -> str:
    """Describe a WebSocket failure without exposing credentials in its URL."""
    details = re.sub(
        r"(?i)wss?://\S+",
        "[WebSocket URL redacted]",
        str(error),
    )
    details = safe_error_text(details, max_chars=1_200)

    status_code = getattr(error, "status_code", None)
    status_message = getattr(error, "status_message", None)
    if status_code is not None:
        status = f"HTTP {status_code}"
        if status_message:
            status += f" {safe_error_text(str(status_message), max_chars=200)}"
        return f"WebSocket handshake rejected ({status}). {details}".strip()

    return f"{type(error).__name__}: {details}"


class TxityClient:
    """Perform network I/O off the GUI thread and publish results via a queue."""

    def __init__(self) -> None:
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.stop_event = threading.Event()
        self.refresh_lock = threading.Lock()
        self.auth_lock = threading.Lock()
        self.reconnect_event = threading.Event()
        self.session_id = str(uuid.uuid4())
        self.session_secret = secrets.token_hex(32)
        self.auth_token = ""
        self.discord_user: dict[str, str] | None = None
        self.ws: Any = None
        self.ws_thread: threading.Thread | None = None
        self.control_send_lock = threading.Lock()

    def set_discord_auth(self, token: Any, user: Any = None) -> None:
        """Attach or disconnect a token received through TXITY's OAuth callback."""
        clean_token = token if is_auth_token(token) else ""
        clean_user = clean_discord_user(user) if clean_token else None
        with self.auth_lock:
            if clean_token == self.auth_token:
                self.discord_user = clean_user
                return
            had_auth = bool(self.auth_token)
            active_socket = self.ws
            self.auth_token = clean_token
            self.discord_user = clean_user

        if active_socket is not None and had_auth and not clean_token:
            try:
                if active_socket.sock and active_socket.sock.connected:
                    active_socket.send(json.dumps({"type": "discordLogout", "discordLogout": True}))
            except Exception:
                pass

        self.reconnect_event.set()
        if active_socket is not None:
            try:
                active_socket.close()
            except Exception:
                pass

    def transmission_credentials(self) -> tuple[str, str, str]:
        """Return the in-memory credentials needed for TXITY's audio socket."""
        with self.auth_lock:
            if not self.auth_token:
                raise RuntimeError("Sign in with Discord before starting transmission.")
            return self.session_id, self.session_secret, self.auth_token

    def control_connected(self) -> bool:
        with self.auth_lock:
            active_socket = self.ws
            if not self.auth_token or active_socket is None:
                return False
            socket = getattr(active_socket, "sock", None)
            return bool(socket is not None and getattr(socket, "connected", False))

    def send_control_message(self, message: dict[str, Any]) -> None:
        """Send a TXITY config or PTT message over the authenticated control socket."""
        with self.auth_lock:
            active_socket = self.ws
            authenticated = bool(self.auth_token)
        if not authenticated:
            raise RuntimeError("Sign in with Discord before starting transmission.")
        if active_socket is None:
            raise ConnectionError("TXITY control connection is not open.")

        with self.control_send_lock:
            socket = getattr(active_socket, "sock", None)
            if socket is None or not getattr(socket, "connected", False):
                raise ConnectionError("TXITY control connection is not ready.")
            active_socket.send(json.dumps(message, separators=(",", ":")))

    def connect(self) -> None:
        if self.ws_thread and self.ws_thread.is_alive():
            if self.stop_event.is_set():
                self.stop_event.clear()
                self.reconnect_event.set()
                if self.ws is not None:
                    try:
                        self.ws.close()
                    except Exception:
                        pass
            return
        self.stop_event.clear()
        self.reconnect_event.clear()
        self.events.put({"kind": "connection", "text": "Connecting to TXITY..."})
        threading.Thread(target=self._fetch_public_data, daemon=True, name="txity-http").start()
        self.ws_thread = threading.Thread(
            target=self._run_status_socket, daemon=True, name="txity-status"
        )
        self.ws_thread.start()

    def load_public_data(self) -> None:
        """Fetch the public configuration and station listing without a socket."""
        threading.Thread(
            target=self._fetch_public_data,
            daemon=True,
            name="txity-public-data",
        ).start()

    def disconnect(self) -> None:
        self.stop_event.set()
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                pass
        self.events.put({"kind": "connection", "text": "Disconnected"})

    def refresh_stations(self) -> None:
        if not self.refresh_lock.acquire(blocking=False):
            return

        def worker() -> None:
            try:
                payload = json.loads(fetch_text(f"{BASE_URL}/users.json"))
                metrics, stations = parse_users_listing(payload)
                self.events.put({
                    "kind": "users_listing",
                    "metrics": metrics,
                    "rows": [station.as_record() for station in stations],
                })
            except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError) as exc:
                self.events.put({
                    "kind": "stations_error",
                    "text": f"Station refresh failed: {safe_error_text(str(exc))}",
                })
            finally:
                self.refresh_lock.release()

        threading.Thread(target=worker, daemon=True, name="txity-stations").start()

    def _fetch_public_data(self) -> None:
        try:
            config = json.loads(fetch_text(f"{BASE_URL}/api/config"))
            if isinstance(config, dict):
                self.events.put({"kind": "config", "data": config})
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            self.events.put({"kind": "error", "text": f"Public configuration unavailable: {exc}"})
        self.refresh_stations()

    def _run_status_socket(self) -> None:
        import websocket

        # Reuse this app session across reconnects. TXITY itself supplies the
        # signed token after the official Discord flow completes.

        status_received = threading.Event()

        def on_open(ws: websocket.WebSocketApp) -> None:
            self.events.put({
                "kind": "connection",
                "text": "Connected (Discord session supplied)" if auth_token else "Connected (public status only)",
            })
            # The status request is read-only. No config, PTT, or audio messages
            # are sent by this desktop client.
            with self.control_send_lock:
                ws.send(json.dumps({"type": "status"}))

            def heartbeat() -> None:
                while not self.stop_event.wait(1.0):
                    try:
                        with self.control_send_lock:
                            ws.send(json.dumps({"type": "status"}))
                    except Exception:
                        return

            threading.Thread(target=heartbeat, daemon=True, name="txity-status-heartbeat").start()

        def on_message(_ws: websocket.WebSocketApp, message: str | bytes) -> None:
            if not isinstance(message, str):
                return
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                return
            if not isinstance(data, dict):
                return
            message_type = data.get("type")
            if message_type == "status":
                if not status_received.is_set():
                    status_received.set()
                    self.events.put({
                        "kind": "connection",
                        "text": "Connected; receiving live TXITY status.",
                    })
                self.events.put({"kind": "status", "data": data})
            elif message_type in {"notice", "kicked", "banned"}:
                self.events.put({"kind": "tx_server_message", "data": data})

        def on_error(_ws: websocket.WebSocketApp, error: Any) -> None:
            if self.stop_event.is_set():
                return
            details = describe_status_socket_error(error)
            write_diagnostic_text("status-websocket-error", details)
            self.events.put({"kind": "socket_error", "text": details})

        def on_close(_ws: websocket.WebSocketApp, code: Any, reason: Any) -> None:
            if self.stop_event.is_set():
                return
            close_text = f"Status WebSocket closed (code: {code!r}"
            if reason:
                close_text += f", reason: {safe_error_text(str(reason), max_chars=300)}"
            close_text += ")."
            self.events.put({"kind": "connection", "text": close_text})

        try:
            while not self.stop_event.is_set():
                with self.auth_lock:
                    auth_token = self.auth_token
                query_values = {
                    "session": self.session_id,
                    "sessionSecret": self.session_secret,
                }
                if auth_token:
                    query_values["authToken"] = auth_token
                socket_url = f"{CONTROL_URL}?{urlencode(query_values)}"
                ws = websocket.WebSocketApp(
                    socket_url,
                    on_open=on_open,
                    on_message=on_message,
                    on_error=on_error,
                    on_close=on_close,
                )
                with self.auth_lock:
                    current_token = self.auth_token
                    self.ws = ws
                if current_token != auth_token:
                    self.reconnect_event.set()
                    ws.close()
                ws.run_forever(
                    origin=BASE_URL,
                    ping_interval=25,
                    ping_timeout=10,
                    reconnect=0,
                )
                if self.ws is ws:
                    self.ws = None
                if not self.stop_event.is_set():
                    self.events.put({"kind": "connection", "text": "Reconnecting status connection..."})
                    if self.reconnect_event.wait(4):
                        self.reconnect_event.clear()
        except Exception as exc:
            if not self.stop_event.is_set():
                details = describe_status_socket_error(exc)
                write_diagnostic_text("status-websocket-client-error", details)
                self.events.put({"kind": "socket_error", "text": details})
        finally:
            self.ws = None




def _system_theme_mode() -> str:
    """Read the Windows application theme preference when available."""
    if sys.platform == "win32":
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            )
            value, _kind = winreg.QueryValueEx(key, "AppsUseLightTheme")
            winreg.CloseKey(key)
            return "light" if value else "dark"
        except (OSError, ImportError):
            pass
    return "light"


def build_app() -> None:
    import tkinter as tk
    from tkinter import ttk
    from material_theme import apply_tk_theme, native_widget_classes

    SurfaceFrame, FlatButton = native_widget_classes(tk)

    client = TxityClient()
    auth_manager = DiscordAuthManager(client.events)
    transmitter = TxityTransmitter(client, client.events)

    theme_mode = _system_theme_mode()

    root = tk.Tk()
    palette = apply_tk_theme(root, theme_mode)
    root.title("TXITPY | TXITY Desktop")
    root.geometry("1180x790")
    root.minsize(900, 650)
    root.configure(background=palette["background"])

    # Tk variables live for the lifetime of the root, even when page widgets
    # are rebuilt after navigation or a theme change.
    connection_var = tk.StringVar(root, "Disconnected")
    account_var = tk.StringVar(root, "Not signed in to Discord")
    auth_status_var = tk.StringVar(root, "Checking TXITY server configuration...")
    center_var = tk.StringVar(root, "Not reported")
    band_var = tk.StringVar(root, "Not reported")
    sample_rate_var = tk.StringVar(root, "Not reported")
    transmitters_var = tk.StringVar(root, "Not reported")
    clients_var = tk.StringVar(root, "Not reported")
    sessions_var = tk.StringVar(root, "Not reported")
    web_audio_var = tk.StringVar(root, "Not reported")
    sdrpp_streaming_var = tk.StringVar(root, "Not reported")
    updated_var = tk.StringVar(root, "Waiting for server data")
    summary_var = tk.StringVar(root, "Sign in with Discord to load TXITY server information.")
    station_status_var = tk.StringVar(root, "Waiting for TXITY station data.")
    station_count_var = tk.StringVar(root, "0 stations")

    tx_station_name_var = tk.StringVar(root, "")
    tx_description_var = tk.StringVar(root, "")
    tx_frequency_var = tk.StringVar(root, "7.250 MHz")
    tx_frequency_check_var = tk.StringVar(root, "Waiting for TXITY station data.")
    tx_mode_var = tk.StringVar(root, "AM")
    tx_mode_help_var = tk.StringVar(root, "AM uses a carrier with upper and lower sidebands.")
    tx_power_var = tk.StringVar(root, "50")
    tx_audio_source_var = tk.StringVar(root, "Microphone / line input")
    tx_audio_device_var = tk.StringVar(root, "System default")
    tx_audio_device_label_var = tk.StringVar(root, "Input device")
    tx_audio_input_var = tx_audio_device_var
    tx_cw_text_var = tk.StringVar(root, "CQ CQ DE TXITY")
    tx_cw_wpm_var = tk.StringVar(root, "18")
    tx_cw_loop_var = tk.BooleanVar(root, True)
    tx_allow_overlap_var = tk.BooleanVar(root, False)
    tx_logo_url_var = tk.StringVar(root, "")
    tx_details_var = tk.StringVar(root, "")
    tx_page_layout_var = tk.StringVar(root, "")
    tx_status_var = tk.StringVar(root, "Not transmitting")
    tx_metrics_var = tk.StringVar(root, "Audio frames sent: 0")
    tx_audio_device_ids: dict[str, int | None] = {"System default": None}
    tx_transmit_button: tk.Button | None = None
    tx_profile_initialized = False

    metric_vars = {
        "Center frequency": center_var,
        "Frequency range": band_var,
        "Sample rate": sample_rate_var,
        "Active transmitters": transmitters_var,
        "Connected clients": clients_var,
        "Total sessions": sessions_var,
        "Web audio clients": web_audio_var,
        "SDR++ streaming": sdrpp_streaming_var,
    }

    discord_authenticated = False
    auth_restore_started = False
    auth_enabled = False
    users_listing_received = False
    station_rows: list[dict[str, Any]] = []
    server_band_min_hz: int | None = None
    server_band_max_hz: int | None = None
    tx_negative_status_reason: str | None = None
    tx_negative_status_count = 0
    station_search_var = tk.StringVar(root, "")
    station_cards_frame: tk.Frame | None = None
    selected_station_id = ""
    image_cache: dict[tuple[str, int], Any] = {}
    image_loading: set[str] = set()
    image_labels: dict[str, list[tuple[tk.Label, int, str]]] = {}
    current_page = "overview"
    shell_frame: tk.Frame | None = None
    content_frame: tk.Frame | None = None
    nav_frame: tk.Frame | None = None
    active_scroll_canvas: tk.Canvas | None = None
    wheel_remainder = 0.0

    def scroll_active_canvas(event, direction: int | None = None):
        nonlocal wheel_remainder
        canvas = active_scroll_canvas
        if canvas is None:
            return

        try:
            if not canvas.winfo_exists():
                return
            left = canvas.winfo_rootx()
            top = canvas.winfo_rooty()
            right = left + canvas.winfo_width()
            bottom = top + canvas.winfo_height()
            if not (left <= event.x_root < right and top <= event.y_root < bottom):
                return
        except tk.TclError:
            return

        if direction is not None:
            canvas.yview_scroll(direction * 3, "units")
            return "break"

        delta = getattr(event, "delta", 0)
        if not delta:
            return
        wheel_remainder += -delta / 120.0
        units = int(wheel_remainder)
        if units:
            canvas.yview_scroll(units, "units")
            wheel_remainder -= units
        return "break"

    root.bind_all("<MouseWheel>", scroll_active_canvas, add="+")
    root.bind_all("<Button-4>", lambda event: scroll_active_canvas(event, -1), add="+")
    root.bind_all("<Button-5>", lambda event: scroll_active_canvas(event, 1), add="+")

    def apply_current_theme() -> None:
        nonlocal palette
        palette = apply_tk_theme(root, theme_mode)
        root.configure(background=palette["background"])

    def make_label(
        parent: tk.Misc,
        text: str | None = None,
        *,
        variable: tk.StringVar | None = None,
        font: tuple[str, int, str] = ("Segoe UI", 10, "normal"),
        color: str | None = None,
        background: str | None = None,
        wraplength: int = 0,
        justify: str = "left",
        anchor: str = "w",
    ) -> tk.Label:
        options: dict[str, Any] = {
            "font": font,
            "fg": color or palette["onSurface"],
            "bg": background or palette["surface"],
            "anchor": anchor,
            "justify": justify,
            "borderwidth": 0,
            "highlightthickness": 0,
        }
        if variable is not None:
            options["textvariable"] = variable
        else:
            options["text"] = text or ""
        if wraplength:
            options["wraplength"] = wraplength
        return tk.Label(parent, **options)

    def make_button(
        parent: tk.Misc,
        text: str,
        command: Any,
        *,
        kind: str = "primary",
        padx: int = 16,
        pady: int = 9,
    ) -> tk.Widget:
        outside = getattr(parent, "_surface_background", parent.cget("bg"))
        if kind == "primary":
            background = palette["primary"]
            foreground = palette["onPrimary"]
            hover_background = palette["primaryContainer"]
            hover_foreground = palette["onPrimaryContainer"]
        elif kind == "tonal":
            background = palette["secondaryContainer"]
            foreground = palette["onSecondaryContainer"]
            hover_background = palette["surfaceContainerHighest"]
            hover_foreground = palette["onSurface"]
        elif kind == "text":
            background = outside
            foreground = palette["primary"]
            hover_background = palette["surfaceContainerHigh"]
            hover_foreground = palette["primary"]
        else:
            background = palette["surfaceContainerLow"]
            foreground = palette["onSurface"]
            hover_background = palette["surfaceContainerHigh"]
            hover_foreground = palette["onSurface"]
        return FlatButton(
            parent,
            text,
            command,
            background=background,
            foreground=foreground,
            hover_background=hover_background,
            hover_foreground=hover_foreground,
            outside=outside,
            outline=background,
            focus_color=palette["primary"],
            font=("Segoe UI", 10, "bold" if kind == "primary" else "normal"),
            padding_x=padx,
            padding_y=pady,
        )

    def make_card(
        parent: tk.Misc,
        *,
        background: str | None = None,
        padding: int = 16,
    ) -> tk.Frame:
        surface_color = background or palette["surfaceContainerLow"]
        return SurfaceFrame(
            parent,
            background=surface_color,
            outside=parent.cget("bg"),
            outline=palette["outlineVariant"],
            padding=padding,
        )

    def request_station_image(url: str) -> None:
        if not url:
            return
        display_url = describe_media_url(url)
        if url in image_loading:
            print(
                f"[TXITPY] Image request reused while loading: {display_url}",
                file=sys.stderr,
                flush=True,
            )
            return

        image_loading.add(url)
        print(
            f"[TXITPY] Image request started: {display_url}",
            file=sys.stderr,
            flush=True,
        )

        def worker() -> None:
            try:
                data = fetch_station_image(url)
                client.events.put({
                    "kind": "station_image",
                    "url": url,
                    "data": data,
                    "byte_count": len(data),
                })
            except Exception as exc:
                client.events.put({
                    "kind": "station_image_error",
                    "url": url,
                    "text": safe_error_text(str(exc), max_chars=500),
                })

        threading.Thread(
            target=worker,
            daemon=True,
            name="txity-station-image",
        ).start()

    def make_image_label(
        parent: tk.Misc,
        url: str,
        *,
        size: int,
        placeholder: str = "TX",
        role: str = "station image",
    ) -> tk.Label:
        surface = parent.cget("bg")
        label = tk.Label(
            parent,
            text=placeholder,
            font=("Segoe UI", max(16, size // 5), "bold"),
            fg=palette["onSurfaceVariant"],
            bg=palette["surfaceContainerHigh"],
            width=max(5, size // 9),
            height=max(3, size // 18),
            relief="flat",
            borderwidth=0,
            anchor="center",
        )
        if not url:
            print(
                f"[TXITPY] Image skipped: {role} has no image URL.",
                file=sys.stderr,
                flush=True,
            )
            return label

        key = (url, size)
        photo = image_cache.get(key)
        if photo is not None:
            label.configure(image=photo, text="", width=size, height=size, bg=surface)
            label.image = photo
            print(
                f"[TXITPY] Image cache hit: {role} ({describe_media_url(url)})",
                file=sys.stderr,
                flush=True,
            )
            return label

        image_labels.setdefault(url, []).append((label, size, role))
        request_station_image(url)
        return label

    def apply_station_image(url: str, data: bytes) -> None:
        image_loading.discard(url)
        waiting = image_labels.pop(url, [])
        display_url = describe_media_url(url)
        try:
            from io import BytesIO
            from PIL import Image, ImageOps, ImageTk

            source = Image.open(BytesIO(data))
            source.load()
            source = source.convert("RGBA")
        except Exception as exc:
            print(
                f"[TXITPY] Image decode error: {display_url}: "
                f"{safe_error_text(str(exc), 300)}",
                file=sys.stderr,
                flush=True,
            )
            return

        print(
            f"[TXITPY] Image downloaded: {display_url} "
            f"({len(data)} bytes, {source.width}x{source.height})",
            file=sys.stderr,
            flush=True,
        )
        applied = 0
        for label, size, role in waiting:
            try:
                if not label.winfo_exists():
                    print(
                        f"[TXITPY] Image target discarded: {role} widget no longer exists "
                        f"({display_url})",
                        file=sys.stderr,
                        flush=True,
                    )
                    continue
                image = ImageOps.fit(
                    source,
                    (size, size),
                    method=Image.Resampling.LANCZOS,
                    centering=(0.5, 0.5),
                )
                photo = ImageTk.PhotoImage(image)
                image_cache[(url, size)] = photo
                label.configure(
                    image=photo,
                    text="",
                    width=size,
                    height=size,
                    bg=label.cget("bg"),
                )
                label.image = photo
                applied += 1
                print(
                    f"[TXITPY] Image applied: {role} ({size}x{size}, {display_url})",
                    file=sys.stderr,
                    flush=True,
                )
            except Exception as exc:
                print(
                    f"[TXITPY] Image apply error: {role} ({display_url}): "
                    f"{safe_error_text(str(exc), 200)}",
                    file=sys.stderr,
                    flush=True,
                )

        if waiting and not applied:
            print(
                f"[TXITPY] Image downloaded but not displayed: {display_url}",
                file=sys.stderr,
                flush=True,
            )

    def bind_station_card(widget: tk.Widget, station_id: str) -> None:
        widget.configure(cursor="hand2")
        widget.bind(
            "<Button-1>",
            lambda _event, target=station_id: open_station(target),
            add="+",
        )
        for child in widget.winfo_children():
            bind_station_card(child, station_id)

    def set_auth_status(message: str) -> None:
        auth_status_var.set(message)

    def on_sign_in() -> None:
        if not auth_enabled:
            set_auth_status(
                "Discord sign-in is unavailable because TXITY has not enabled secure authentication."
            )
            return
        auth_manager.start_login()

    def disconnect_discord() -> None:
        nonlocal discord_authenticated, current_page
        transmitter.stop(wait=0.5)
        client.set_discord_auth(None, None)
        client.disconnect()
        discord_authenticated = False
        profile_cleared = auth_manager.clear_browser_session()
        account_var.set("Not connected to Discord")
        set_auth_status(
            "Discord was disconnected and the saved browser session was cleared."
            if profile_cleared
            else "Discord was disconnected, but the saved browser session could not be cleared."
        )
        current_page = "overview"
        build_shell()

    def on_shutdown() -> None:
        transmitter.stop(wait=1.0)
        client.disconnect()
        auth_manager.close()
        root.destroy()

    def disconnect_status_connection() -> None:
        transmitter.stop(wait=0.5)
        client.disconnect()

    def toggle_theme() -> None:
        nonlocal theme_mode
        theme_mode = "dark" if theme_mode == "light" else "light"
        apply_current_theme()
        build_shell()

    def navigate(page: str) -> None:
        nonlocal current_page
        if not discord_authenticated and page != "discord":
            current_page = "discord"
            build_shell()
            return
        current_page = page
        build_shell()

    def open_station(station_id: str) -> None:
        nonlocal selected_station_id, current_page
        selected_station_id = station_id
        current_page = "station_detail"
        build_shell()

    def update_station_tree() -> None:
        """Refresh the visible station-card grid."""
        if station_cards_frame is None:
            return
        try:
            if not station_cards_frame.winfo_exists():
                return
        except tk.TclError:
            return

        for child in station_cards_frame.winfo_children():
            child.destroy()

        query = station_search_var.get().strip().casefold()
        visible = []
        for row in station_rows:
            searchable = " ".join(str(value) for value in (
                row.get("station", ""),
                row.get("operator", ""),
                row.get("frequency", ""),
                row.get("mode", ""),
                row.get("description", ""),
            )).casefold()
            if not query or query in searchable:
                visible.append(row)

        station_count_var.set(
            f"{len(visible)} of {len(station_rows)} stations"
            if query else f"{len(station_rows)} stations"
        )

        if not visible:
            empty = make_card(station_cards_frame, padding=16)
            empty.pack(fill="x", padx=6, pady=6)
            message = "No stations match that search." if query else "No stations are currently listed."
            make_label(
                empty,
                message,
                color=palette["onSurfaceVariant"],
                background=palette["surfaceContainerLow"],
            ).pack(anchor="w")
            return

        for column in range(2):
            station_cards_frame.grid_columnconfigure(column, weight=1, uniform="station-card")

        for index, row in enumerate(visible):
            station_id = str(row.get("id", ""))
            card = make_card(station_cards_frame, padding=12)
            card.grid(
                row=index // 2,
                column=index % 2,
                sticky="nsew",
                padx=6,
                pady=6,
            )
            card.configure(cursor="hand2")

            content = tk.Frame(card, bg=palette["surfaceContainerLow"])
            content.pack(fill="both", expand=True)

            logo_url = row.get("logo_url") or ""
            image = make_image_label(content, logo_url, size=112, role=f"station card logo: {row.get('station', 'unknown')}")
            image.pack(side="left", padx=(0, 14), pady=4)

            details = tk.Frame(content, bg=palette["surfaceContainerLow"])
            details.pack(side="left", fill="both", expand=True)

            make_label(
                details,
                str(row.get("station", "Unknown station")),
                font=("Segoe UI", 13, "bold"),
                background=palette["surfaceContainerLow"],
            ).pack(anchor="w")
            description = str(row.get("description") or "No description yet.")
            make_label(
                details,
                description,
                font=("Segoe UI", 9, "normal"),
                color=palette["onSurfaceVariant"],
                background=palette["surfaceContainerLow"],
                wraplength=260,
            ).pack(anchor="w", pady=(4, 7))
            make_label(
                details,
                f"Discord: {row.get('operator') or 'Unknown'}",
                font=("Segoe UI", 8, "normal"),
                color=palette["onSurfaceVariant"],
                background=palette["surfaceContainerLow"],
            ).pack(anchor="w")

            meta = tk.Frame(content, bg=palette["surfaceContainerLow"])
            meta.pack(side="right", fill="y", padx=(10, 2))
            make_label(
                meta,
                str(row.get("frequency") or "Frequency unknown"),
                font=("Segoe UI", 12, "bold"),
                color=palette["primary"],
                background=palette["surfaceContainerLow"],
            ).pack(anchor="e", pady=(4, 8))
            make_label(
                meta,
                str(row.get("rating") or "Not rated"),
                font=("Segoe UI", 9, "bold"),
                background=palette["surfaceContainerLow"],
            ).pack(anchor="e", pady=(0, 6))
            make_label(
                meta,
                f"{row.get('mode') or 'Mode unknown'} · {row.get('on_air') or 'On-air time unknown'}",
                font=("Segoe UI", 8, "normal"),
                color=palette["onSurfaceVariant"],
                background=palette["surfaceContainerLow"],
                justify="right",
            ).pack(anchor="e")

            bind_station_card(card, station_id)

    def refresh_station_list() -> None:
        if not discord_authenticated:
            navigate("discord")
            return
        station_status_var.set("Refreshing station data from TXITY...")
        client.refresh_stations()

    def show_gate(parent: tk.Frame) -> None:
        outer = tk.Frame(parent, bg=palette["background"])
        outer.pack(fill="both", expand=True)

        card = make_card(
            outer,
            background=palette["surfaceContainerLow"],
            padding=24,
        )
        card.place(relx=0.5, rely=0.46, anchor="center", width=520)

        make_label(
            card,
            "TXITY",
            font=("Segoe UI", 30, "bold"),
            background=palette["surfaceContainerLow"],
            color=palette["primary"],
        ).pack(anchor="w")
        make_label(
            card,
            "Sign in with Discord",
            font=("Segoe UI", 21, "bold"),
            background=palette["surfaceContainerLow"],
        ).pack(anchor="w", pady=(14, 6))
        make_label(
            card,
            "Your Discord account is required to use the TXITY desktop dashboard. "
            "TXITPY checks your saved session automatically on later launches.",
            font=("Segoe UI", 10, "normal"),
            color=palette["onSurfaceVariant"],
            background=palette["surfaceContainerLow"],
            wraplength=440,
        ).pack(anchor="w", pady=(0, 20))

        make_label(
            card,
            variable=account_var,
            font=("Segoe UI", 11, "bold"),
            background=palette["surfaceContainerLow"],
        ).pack(anchor="w", pady=(0, 8))

        status_label = make_label(
            card,
            variable=auth_status_var,
            font=("Segoe UI", 10, "normal"),
            color=palette["onSurfaceVariant"],
            background=palette["surfaceContainerLow"],
            wraplength=440,
        )
        status_label.pack(anchor="w", pady=(0, 18))

        make_button(
            card,
            "Sign in with Discord",
            on_sign_in,
            kind="primary",
            padx=20,
            pady=11,
        ).pack(anchor="w")

        make_label(
            card,
            "TXITY authentication is handled by TXITY's official Discord sign-in page.",
            font=("Segoe UI", 9, "normal"),
            color=palette["onSurfaceVariant"],
            background=palette["surfaceContainerLow"],
            wraplength=440,
        ).pack(anchor="w", pady=(20, 0))

    def create_scrollable_page(parent: tk.Frame) -> tuple[tk.Frame, tk.Canvas]:
        nonlocal active_scroll_canvas
        canvas = tk.Canvas(
            parent,
            background=palette["background"],
            highlightthickness=0,
            borderwidth=0,
            yscrollincrement=28,
        )
        page = tk.Frame(canvas, background=palette["background"])
        window_id = canvas.create_window((0, 0), window=page, anchor="nw")

        def update_region(_event=None) -> None:
            bounds = canvas.bbox("all")
            if bounds:
                canvas.configure(scrollregion=bounds)
            canvas.itemconfigure(window_id, width=canvas.winfo_width())

        page.bind("<Configure>", update_region)
        canvas.bind("<Configure>", update_region)
        canvas.pack(fill="both", expand=True)
        active_scroll_canvas = canvas
        return page, canvas

    def show_overview(parent: tk.Frame) -> None:
        page, canvas = create_scrollable_page(parent)

        make_label(
            page,
            "TXITY",
            font=("Segoe UI", 26, "bold"),
            background=palette["background"],
        ).pack(anchor="w", pady=(8, 0))
        make_label(
            page,
            "Public server monitor and station directory",
            font=("Segoe UI", 11, "normal"),
            color=palette["onSurfaceVariant"],
            background=palette["background"],
        ).pack(anchor="w", pady=(2, 18))

        actions = tk.Frame(page, bg=palette["background"])
        actions.pack(fill="x", pady=(0, 18))
        make_button(actions, "Connect", client.connect, kind="primary").pack(side="left", padx=(0, 8))
        make_button(actions, "Disconnect", disconnect_status_connection, kind="tonal").pack(side="left", padx=(0, 8))
        make_button(
            actions,
            "Open TXITY",
            lambda: webbrowser.open(BASE_URL),
            kind="text",
        ).pack(side="left")

        make_label(
            page,
            "SERVER STATUS",
            font=("Segoe UI", 10, "bold"),
            color=palette["onSurfaceVariant"],
            background=palette["background"],
        ).pack(anchor="w", pady=(0, 10))

        grid = tk.Frame(page, bg=palette["background"])
        grid.pack(fill="x")
        for column in range(4):
            grid.grid_columnconfigure(column, weight=1, uniform="metric")

        for index, (name, variable) in enumerate(metric_vars.items()):
            row, column = divmod(index, 4)
            card = make_card(
                grid,
                background=palette["surfaceContainerLow"],
                padding=16,
            )
            card.grid(
                row=row,
                column=column,
                sticky="nsew",
                padx=6,
                pady=6,
            )
            make_label(
                card,
                name,
                font=("Segoe UI", 9, "bold"),
                color=palette["onSurfaceVariant"],
                background=palette["surfaceContainerLow"],
            ).pack(anchor="w")
            make_label(
                card,
                variable=variable,
                font=("Segoe UI", 21, "bold"),
                background=palette["surfaceContainerLow"],
            ).pack(anchor="w", pady=(7, 0))

        info = make_card(page, padding=16)
        info.pack(fill="x", pady=(8, 12))
        make_label(
            info,
            variable=summary_var,
            font=("Segoe UI", 10, "normal"),
            background=palette["surfaceContainerLow"],
            wraplength=850,
        ).pack(anchor="w")
        make_label(
            info,
            variable=updated_var,
            font=("Segoe UI", 9, "normal"),
            color=palette["onSurfaceVariant"],
            background=palette["surfaceContainerLow"],
        ).pack(anchor="w", pady=(9, 0))

    def show_stations(parent: tk.Frame) -> None:
        nonlocal station_cards_frame

        page, canvas = create_scrollable_page(parent)
        make_label(
            page,
            "Stations",
            font=("Segoe UI", 27, "bold"),
            background=palette["background"],
        ).pack(anchor="w", pady=(6, 2))
        make_label(
            page,
            "Live transmitters running on TXITY.",
            font=("Segoe UI", 10, "normal"),
            color=palette["onSurfaceVariant"],
            background=palette["background"],
        ).pack(anchor="w", pady=(0, 17))

        controls = tk.Frame(page, bg=palette["background"])
        controls.pack(fill="x", pady=(0, 10))

        search_shell = SurfaceFrame(
            controls,
            background=palette["surfaceContainerLowest"],
            outside=palette["background"],
            outline=palette["outlineVariant"],
            padding=5,
            padding_x=22,
            padding_y=5,
        )
        search_shell.pack(side="left", fill="x", expand=True)
        search = tk.Entry(
            search_shell,
            textvariable=station_search_var,
            font=("Segoe UI", 10),
            bg=palette["surfaceContainerLowest"],
            fg=palette["onSurface"],
            insertbackground=palette["onSurface"],
            selectbackground=palette["primaryContainer"],
            selectforeground=palette["onPrimaryContainer"],
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
        )
        search.pack(fill="x", expand=True, ipady=5)
        search.bind("<KeyRelease>", lambda _event: update_station_tree())

        make_button(
            controls,
            "Refresh",
            refresh_station_list,
            kind="primary",
        ).pack(side="left", padx=(10, 0))

        status_bar = tk.Frame(page, bg=palette["background"])
        status_bar.pack(fill="x", pady=(2, 10))
        make_label(
            status_bar,
            variable=station_status_var,
            font=("Segoe UI", 9, "normal"),
            color=palette["onSurfaceVariant"],
            background=palette["background"],
        ).pack(side="left")
        make_label(
            status_bar,
            variable=station_count_var,
            font=("Segoe UI", 9, "bold"),
            color=palette["onSurfaceVariant"],
            background=palette["background"],
        ).pack(side="right")

        station_cards_frame = tk.Frame(page, bg=palette["background"])
        station_cards_frame.pack(fill="x", expand=True)
        update_station_tree()

    def update_frequency_check() -> None:
        try:
            selected_hz = parse_rf_frequency(tx_frequency_var.get())
        except ValueError:
            tx_frequency_check_var.set("Enter a valid frequency to check the TXITY station list.")
            return

        messages: list[str] = []
        if server_band_min_hz is not None and server_band_max_hz is not None:
            if not server_band_min_hz <= selected_hz <= server_band_max_hz:
                messages.append(
                    "Outside the server's reported virtual frequency range "
                    f"({format_frequency(server_band_min_hz)} to {format_frequency(server_band_max_hz)})."
                )

        own_user_id = str((client.discord_user or {}).get("id") or "")
        other_stations = [
            row for row in station_rows
            if not own_user_id or str(row.get("discord_id") or "") != own_user_id
        ]
        overlaps = find_frequency_overlaps(
            tx_frequency_var.get(),
            tx_mode_var.get(),
            other_stations,
        )

        if overlaps:
            names = []
            for item in overlaps[:4]:
                offset_khz = item["offset_hz"] / 1_000
                names.append(
                    f'{item["station_name"]} at {format_frequency(item["frequency_hz"])} '
                    f'({item["mode"]}, {offset_khz:.1f} kHz offset)'
                )
            extra = f" and {len(overlaps) - 4} more" if len(overlaps) > 4 else ""
            if not tx_allow_overlap_var.get():
                sharing_note = "Frequency sharing is off, so TXITY may reject this overlap."
            elif any(item["allow_overlap_visitors"] is False for item in overlaps):
                sharing_note = "At least one overlapping station reports sharing disabled."
            elif all(item["allow_overlap_visitors"] is True for item in overlaps):
                sharing_note = "The listed stations report sharing enabled."
            else:
                sharing_note = "Remote sharing settings are not fully public; TXITY makes the final decision."
            messages.append(
                "Estimated overlap with " + "; ".join(names) + extra + ". " + sharing_note
            )
        elif station_rows:
            messages.append("No estimated channel overlap with the other listed TXITY stations.")
        else:
            messages.append("Station overlap check will run when TXITY's live station list loads.")

        messages.append("This is a client-side estimate; TXITY's server decides whether a shared frequency is accepted.")
        tx_frequency_check_var.set(" ".join(messages))

    tx_frequency_var.trace_add("write", lambda *_args: update_frequency_check())
    tx_mode_var.trace_add("write", lambda *_args: update_frequency_check())
    tx_allow_overlap_var.trace_add("write", lambda *_args: update_frequency_check())

    def update_transmit_button() -> None:
        button = tx_transmit_button
        if button is None:
            return
        try:
            if button.winfo_exists():
                button.configure(
                    text="Stop Transmission" if transmitter.is_busy else "Start Transmission"
                )
        except tk.TclError:
            pass

    def toggle_transmission() -> None:
        if transmitter.is_busy:
            transmitter.stop()
            tx_status_var.set("Stopping transmission...")
            update_transmit_button()
            return
        if not discord_authenticated:
            navigate("discord")
            return

        try:
            config = build_transmit_config(
                station_name=tx_station_name_var.get(),
                description=tx_description_var.get(),
                frequency=tx_frequency_var.get(),
                mode=tx_mode_var.get(),
                station_dbm=tx_power_var.get(),
                allow_overlap=tx_allow_overlap_var.get(),
                auth_token=client.auth_token,
                cw_text=tx_cw_text_var.get(),
                cw_wpm=tx_cw_wpm_var.get(),
                cw_loop=tx_cw_loop_var.get(),
                station_logo_url=tx_logo_url_var.get(),
                station_details=tx_details_var.get(),
                station_page_layout=tx_page_layout_var.get(),
            )
            audio_source = (
                "loopback"
                if tx_audio_source_var.get() == "Desktop audio (output loopback)"
                else "mic"
            )
            selected_device = tx_audio_device_ids.get(tx_audio_device_var.get())
            if config["mode"] != "CW" and selected_device is None and audio_source == "loopback":
                raise ValueError("Select a desktop output device for loopback capture.")
            transmitter.start(
                config,
                input_device=selected_device,
                audio_source=audio_source,
            )
            tx_status_var.set("Preparing transmission...")
            update_transmit_button()
        except Exception as exc:
            details = safe_error_text(str(exc), 500)
            tx_status_var.set(details)
            print(f"[TXITPY] Transmission start rejected: {details}", file=sys.stderr, flush=True)

    def show_transmit(parent: tk.Frame) -> None:
        nonlocal tx_transmit_button

        page, _canvas = create_scrollable_page(parent)

        hero = tk.Frame(page, bg=palette["background"])
        hero.pack(fill="x", pady=(6, 16))
        make_label(
            hero,
            "TXITY",
            font=("Segoe UI", 28, "bold"),
            background=palette["background"],
        ).pack(side="left")
        make_label(
            hero,
            "Transmit",
            font=("Segoe UI", 11, "normal"),
            color=palette["onSurfaceVariant"],
            background=palette["background"],
        ).pack(side="left", padx=(12, 0), pady=(9, 0))
        make_label(
            hero,
            variable=connection_var,
            font=("Segoe UI", 9, "normal"),
            color=palette["onSurfaceVariant"],
            background=palette["background"],
        ).pack(side="right", pady=(10, 0))

        def add_tx_entry(
            parent_frame: tk.Misc,
            label: str,
            variable: tk.StringVar,
            *,
            column: int,
            row: int,
        ) -> None:
            wrapper = tk.Frame(parent_frame, bg=palette["surfaceContainerLow"])
            wrapper.grid(row=row, column=column, sticky="ew", padx=6, pady=6)
            make_label(
                wrapper,
                label,
                font=("Segoe UI", 9, "bold"),
                color=palette["onSurfaceVariant"],
                background=palette["surfaceContainerLow"],
            ).pack(anchor="w", pady=(0, 4))
            entry = tk.Entry(
                wrapper,
                textvariable=variable,
                font=("Segoe UI", 10),
                bg=palette["surfaceContainerLowest"],
                fg=palette["onSurface"],
                insertbackground=palette["onSurface"],
                selectbackground=palette["primaryContainer"],
                selectforeground=palette["onPrimaryContainer"],
                relief="flat",
                borderwidth=1,
                highlightthickness=1,
                highlightbackground=palette["outlineVariant"],
                highlightcolor=palette["primary"],
            )
            entry.pack(fill="x", ipady=7)

        def style_tx_combobox(widget: ttk.Combobox) -> None:
            style = ttk.Style(root)
            style.configure(
                "TXITY.Transmit.TCombobox",
                fieldbackground=palette["surfaceContainerLowest"],
                background=palette["surfaceContainerHigh"],
                foreground=palette["onSurface"],
                arrowcolor=palette["onSurface"],
                bordercolor=palette["outlineVariant"],
                lightcolor=palette["outlineVariant"],
                darkcolor=palette["outlineVariant"],
                padding=(10, 8),
            )
            style.map(
                "TXITY.Transmit.TCombobox",
                fieldbackground=[
                    ("readonly", palette["surfaceContainerLowest"]),
                    ("disabled", palette["surfaceContainer"]),
                ],
                foreground=[
                    ("readonly", palette["onSurface"]),
                    ("disabled", palette["onSurfaceVariant"]),
                ],
            )
            widget.configure(style="TXITY.Transmit.TCombobox", font=("Segoe UI", 11))

        station_card = make_card(page, padding=16)
        station_card.pack(fill="x", pady=(0, 12))
        make_label(
            station_card,
            "Station",
            font=("Segoe UI", 15, "bold"),
            background=palette["surfaceContainerLow"],
        ).pack(anchor="w", pady=(0, 10))

        station_fields = tk.Frame(station_card, bg=palette["surfaceContainerLow"])
        station_fields.pack(fill="x")
        station_fields.grid_columnconfigure(0, weight=1, uniform="station-fields")
        station_fields.grid_columnconfigure(1, weight=1, uniform="station-fields")
        add_tx_entry(station_fields, "Station name (required)", tx_station_name_var, column=0, row=0)
        add_tx_entry(station_fields, "Short description", tx_description_var, column=1, row=0)
        add_tx_entry(station_fields, "Transmit frequency", tx_frequency_var, column=0, row=1)
        make_label(
            station_card,
            variable=tx_frequency_check_var,
            font=("Segoe UI", 9, "normal"),
            color=palette["onSurfaceVariant"],
            background=palette["surfaceContainerLow"],
            wraplength=860,
        ).pack(anchor="w", padx=6, pady=(8, 0))
        update_frequency_check()

        signal_card = make_card(page, padding=16)
        signal_card.pack(fill="x", pady=(0, 12))
        make_label(
            signal_card,
            "Signal settings",
            font=("Segoe UI", 15, "bold"),
            background=palette["surfaceContainerLow"],
        ).pack(anchor="w", pady=(0, 10))

        signal_fields = tk.Frame(signal_card, bg=palette["surfaceContainerLow"])
        signal_fields.pack(fill="x")
        signal_fields.grid_columnconfigure(0, weight=3, uniform="signal-fields")
        signal_fields.grid_columnconfigure(1, weight=2, uniform="signal-fields")

        mode_frame = tk.Frame(signal_fields, bg=palette["surfaceContainerLow"])
        mode_frame.grid(row=0, column=0, sticky="ew", padx=6, pady=6)
        make_label(
            mode_frame,
            "Signal type",
            font=("Segoe UI", 10, "bold"),
            color=palette["onSurfaceVariant"],
            background=palette["surfaceContainerLow"],
        ).pack(anchor="w", pady=(0, 5))
        mode_picker = ttk.Combobox(
            mode_frame,
            textvariable=tx_mode_var,
            values=("AM", "USB", "LSB", "USB+C", "LSB+C", "DSB", "CW"),
            state="readonly",
            height=7,
        )
        mode_picker.pack(fill="x", ipady=5)
        style_tx_combobox(mode_picker)

        mode_help = make_label(
            mode_frame,
            variable=tx_mode_help_var,
            font=("Segoe UI", 9, "normal"),
            color=palette["onSurfaceVariant"],
            background=palette["surfaceContainerLow"],
            wraplength=570,
        )
        mode_help.pack(anchor="w", pady=(6, 0))

        def update_mode_help(_event=None) -> None:
            descriptions = {
                "AM": "AM — normal AM with carrier.",
                "USB": "USB — upper sideband.",
                "LSB": "LSB — lower sideband.",
                "USB+C": "USB+C — upper sideband with carrier.",
                "LSB+C": "LSB+C — lower sideband with carrier.",
                "DSB": "DSB — double-sideband suppressed carrier.",
                "CW": "CW — carrier only. The microphone and desktop audio sources are not used.",
            }
            tx_mode_help_var.set(descriptions.get(tx_mode_var.get(), "Choose a signal mode."))
            update_frequency_check()
        mode_picker.bind("<<ComboboxSelected>>", update_mode_help)
        update_mode_help()

        power_frame = tk.Frame(signal_fields, bg=palette["surfaceContainerLow"])
        power_frame.grid(row=0, column=1, sticky="nsew", padx=6, pady=6)
        make_label(
            power_frame,
            "Station power",
            font=("Segoe UI", 10, "bold"),
            color=palette["onSurfaceVariant"],
            background=palette["surfaceContainerLow"],
        ).pack(anchor="w", pady=(0, 6))
        power_scale = tk.Scale(
            power_frame,
            variable=tx_power_var,
            from_=-140,
            to=53,
            resolution=1,
            orient="horizontal",
            showvalue=True,
            highlightthickness=0,
            borderwidth=0,
            relief="flat",
            bg=palette["surfaceContainerLow"],
            fg=palette["onSurface"],
            troughcolor=palette["surfaceContainerHighest"],
            activebackground=palette["primary"],
            font=("Segoe UI", 9),
        )
        power_scale.pack(fill="x", expand=True, pady=(1, 0))

        sharing = tk.Checkbutton(
            signal_card,
            text="Allow shared frequency overlap with stations that also enable sharing",
            variable=tx_allow_overlap_var,
            command=update_frequency_check,
            bg=palette["surfaceContainerLow"],
            fg=palette["onSurface"],
            activebackground=palette["surfaceContainerLow"],
            activeforeground=palette["onSurface"],
            selectcolor=palette["surfaceContainerHighest"],
            highlightthickness=0,
            bd=0,
            anchor="w",
        )
        sharing.pack(anchor="w", padx=6, pady=(8, 0))

        audio_card = make_card(page, padding=16)
        audio_card.pack(fill="x", pady=(0, 12))
        make_label(
            audio_card,
            "Audio source",
            font=("Segoe UI", 15, "bold"),
            background=palette["surfaceContainerLow"],
        ).pack(anchor="w", pady=(0, 10))

        audio_fields = tk.Frame(audio_card, bg=palette["surfaceContainerLow"])
        audio_fields.pack(fill="x")
        audio_fields.grid_columnconfigure(0, weight=1, uniform="audio-fields")
        audio_fields.grid_columnconfigure(1, weight=1, uniform="audio-fields")

        source_frame = tk.Frame(audio_fields, bg=palette["surfaceContainerLow"])
        source_frame.grid(row=0, column=0, sticky="ew", padx=6, pady=6)
        make_label(
            source_frame,
            "Audio source",
            font=("Segoe UI", 9, "bold"),
            color=palette["onSurfaceVariant"],
            background=palette["surfaceContainerLow"],
        ).pack(anchor="w", pady=(0, 5))
        source_picker = ttk.Combobox(
            source_frame,
            textvariable=tx_audio_source_var,
            values=("Microphone / line input", "Desktop audio (output loopback)"),
            state="readonly",
            height=4,
        )
        source_picker.pack(fill="x", ipady=5)
        style_tx_combobox(source_picker)

        device_frame = tk.Frame(audio_fields, bg=palette["surfaceContainerLow"])
        device_frame.grid(row=0, column=1, sticky="ew", padx=6, pady=6)
        make_label(
            device_frame,
            variable=tx_audio_device_label_var,
            font=("Segoe UI", 9, "bold"),
            color=palette["onSurfaceVariant"],
            background=palette["surfaceContainerLow"],
        ).pack(anchor="w", pady=(0, 5))
        device_picker = ttk.Combobox(
            device_frame,
            textvariable=tx_audio_device_var,
            values=("System default",),
            state="readonly",
            height=8,
        )
        device_picker.pack(fill="x", ipady=5)
        style_tx_combobox(device_picker)

        def refresh_audio_devices(_event=None) -> None:
            tx_audio_device_ids.clear()
            selected_source = tx_audio_source_var.get()
            if selected_source == "Desktop audio (output loopback)":
                tx_audio_device_label_var.set("Desktop output device")
                try:
                    devices = list_loopback_devices()
                except Exception as exc:
                    devices = []
                    details = safe_error_text(str(exc), 300)
                    tx_status_var.set(details)
                    print(f"[TXITPY] Desktop audio device discovery failed: {details}", file=sys.stderr, flush=True)
                values = []
                for device_id, device_name in devices:
                    tx_audio_device_ids[device_name] = device_id
                    values.append(device_name)
                if not values:
                    values = ["No desktop loopback devices found"]
                    tx_audio_device_var.set(values[0])
                    device_picker.configure(values=values)
                    return
            else:
                tx_audio_device_label_var.set("Microphone / line input")
                tx_audio_device_ids["System default"] = None
                values = ["System default"]
                try:
                    for device_id, device_name in list_audio_inputs():
                        display = f"{device_id}: {device_name}"
                        tx_audio_device_ids[display] = device_id
                        values.append(display)
                except Exception as exc:
                    details = safe_error_text(str(exc), 300)
                    tx_status_var.set(details)
                    print(f"[TXITPY] Microphone device discovery failed: {details}", file=sys.stderr, flush=True)

            device_picker.configure(values=values)
            if tx_audio_device_var.get() not in tx_audio_device_ids:
                tx_audio_device_var.set(values[0])

        source_picker.bind("<<ComboboxSelected>>", refresh_audio_devices)
        refresh_button = make_button(
            audio_card,
            "Refresh audio devices",
            refresh_audio_devices,
            kind="tonal",
            padx=12,
            pady=7,
        )
        refresh_button.pack(anchor="w", padx=6, pady=(8, 2))

        make_label(
            audio_card,
            "Desktop audio uses Windows WASAPI loopback to capture the selected playback device. "
            "Stereo playback is downmixed to mono and converted to 48 kHz Float32 for TXITY. "
            "Use microphone / line input for a physical input device.",
            font=("Segoe UI", 9, "normal"),
            color=palette["onSurfaceVariant"],
            background=palette["surfaceContainerLow"],
            wraplength=850,
        ).pack(anchor="w", padx=6, pady=(7, 0))

        cw_card = make_card(page, padding=16)
        cw_card.pack(fill="x", pady=(0, 12))
        make_label(
            cw_card,
            "CW message",
            font=("Segoe UI", 15, "bold"),
            background=palette["surfaceContainerLow"],
        ).pack(anchor="w", pady=(0, 8))
        cw_fields = tk.Frame(cw_card, bg=palette["surfaceContainerLow"])
        cw_fields.pack(fill="x")
        cw_fields.grid_columnconfigure(0, weight=3, uniform="cw-fields")
        cw_fields.grid_columnconfigure(1, weight=1, uniform="cw-fields")
        add_tx_entry(cw_fields, "Message", tx_cw_text_var, column=0, row=0)
        add_tx_entry(cw_fields, "Speed (5-60 WPM)", tx_cw_wpm_var, column=1, row=0)
        cw_loop = tk.Checkbutton(
            cw_card,
            text="Repeat CW message",
            variable=tx_cw_loop_var,
            bg=palette["surfaceContainerLow"],
            fg=palette["onSurface"],
            activebackground=palette["surfaceContainerLow"],
            activeforeground=palette["onSurface"],
            selectcolor=palette["surfaceContainerHighest"],
            highlightthickness=0,
            bd=0,
            anchor="w",
        )
        cw_loop.pack(anchor="w", padx=6, pady=(8, 0))

        connect_card = make_card(page, padding=16)
        connect_card.pack(fill="x", pady=(0, 12))
        make_label(
            connect_card,
            "How to connect",
            font=("Segoe UI", 15, "bold"),
            background=palette["surfaceContainerLow"],
        ).pack(anchor="w")
        make_label(
            connect_card,
            "TXITY provides a virtual SDR++ Server feed. Connect your SDR++ client to the "
            "server endpoint shown by TXITY, then start the transmission here. TXITPY sends "
            "audio to TXITY's virtual service and does not control local RF hardware.",
            font=("Segoe UI", 9, "normal"),
            color=palette["onSurfaceVariant"],
            background=palette["surfaceContainerLow"],
            wraplength=850,
        ).pack(anchor="w", pady=(6, 0))

        status_card = make_card(page, padding=16)
        status_card.pack(fill="x", pady=(0, 12))
        make_label(
            status_card,
            variable=tx_status_var,
            font=("Segoe UI", 11, "bold"),
            background=palette["surfaceContainerLow"],
        ).pack(anchor="w")
        make_label(
            status_card,
            variable=tx_metrics_var,
            font=("Segoe UI", 9, "normal"),
            color=palette["onSurfaceVariant"],
            background=palette["surfaceContainerLow"],
        ).pack(anchor="w", pady=(6, 0))

        tx_transmit_button = make_button(
            page,
            "Stop Transmission" if transmitter.is_busy else "Start Transmission",
            toggle_transmission,
            kind="primary",
            padx=24,
            pady=12,
        )
        tx_transmit_button.pack(anchor="w", pady=(0, 12))

        make_label(
            page,
            "TXITPY streams selected audio only to TXITY's virtual service.",
            font=("Segoe UI", 9, "normal"),
            color=palette["onSurfaceVariant"],
            background=palette["background"],
            wraplength=850,
        ).pack(anchor="w", pady=(0, 8))

    def show_station_detail(parent: tk.Frame) -> None:
        row = next(
            (item for item in station_rows if str(item.get("id", "")) == selected_station_id),
            None,
        )
        if row is None:
            make_button(parent, "Back to stations", lambda: navigate("stations"), kind="tonal").pack(anchor="w")
            make_label(
                parent,
                "This station is no longer in the current listing.",
                color=palette["onSurfaceVariant"],
                background=palette["background"],
            ).pack(anchor="w", pady=18)
            return

        page, _canvas = create_scrollable_page(parent)
        make_button(
            page,
            "← Back to stations",
            lambda: navigate("stations"),
            kind="text",
            padx=10,
            pady=7,
        ).pack(anchor="w", pady=(5, 12))

        make_label(
            page,
            "LIVE STATION",
            font=("Segoe UI", 9, "bold"),
            color=palette["primary"],
            background=palette["background"],
        ).pack(anchor="w")
        make_label(
            page,
            str(row.get("station", "Unknown station")),
            font=("Segoe UI", 28, "bold"),
            background=palette["background"],
            wraplength=900,
        ).pack(anchor="w", pady=(2, 1))
        short_description = str(
            row.get("description")
            or row.get("details_text")
            or "No short description has been added."
        )
        make_label(
            page,
            short_description,
            font=("Segoe UI", 11, "normal"),
            color=palette["onSurfaceVariant"],
            background=palette["background"],
            wraplength=900,
        ).pack(anchor="w", pady=(0, 16))

        profile = make_card(page, padding=16)
        profile.pack(fill="x", pady=(0, 12))
        upper = tk.Frame(profile, bg=palette["surfaceContainerLow"])
        upper.pack(fill="x")

        logo = make_image_label(upper, str(row.get("logo_url") or ""), size=176, placeholder="TXITY", role=f"station profile logo: {row.get('station', 'unknown')}")
        logo.pack(side="left", padx=(0, 16), pady=(0, 12))

        heading = tk.Frame(upper, bg=palette["surfaceContainerLow"])
        heading.pack(side="left", fill="both", expand=True)
        make_label(
            heading,
            str(row.get("frequency") or "Frequency not reported"),
            font=("Segoe UI", 18, "bold"),
            color=palette["primary"],
            background=palette["surfaceContainerLow"],
        ).pack(anchor="w", pady=(4, 8))
        make_label(
            heading,
            f"{row.get('mode') or 'Mode not reported'}  ·  {row.get('on_air') or 'On-air time not reported'}",
            font=("Segoe UI", 10, "normal"),
            color=palette["onSurfaceVariant"],
            background=palette["surfaceContainerLow"],
        ).pack(anchor="w", pady=(0, 8))
        make_label(
            heading,
            f"★ {row.get('rating_average'):.2f} ({row.get('rating_count', 0)})"
            if isinstance(row.get("rating_average"), (int, float)) and row.get("rating_count")
            else "Not rated yet",
            font=("Segoe UI", 11, "bold"),
            background=palette["surfaceContainerLow"],
        ).pack(anchor="w")
        make_label(
            heading,
            f"♡ {row.get('likes', 0)} likes",
            font=("Segoe UI", 10, "normal"),
            color=palette["onSurfaceVariant"],
            background=palette["surfaceContainerLow"],
        ).pack(anchor="w", pady=(7, 0))

        divider = tk.Frame(profile, bg=palette["outlineVariant"], height=1)
        divider.pack(fill="x", pady=(4, 14))

        owner = tk.Frame(profile, bg=palette["surfaceContainerLow"])
        owner.pack(fill="x")
        avatar_url = str(row.get("avatar_url") or "")
        avatar = make_image_label(
            owner,
            avatar_url,
            size=42,
            placeholder="D",
            role=f"station owner avatar: {row.get('station', 'unknown')}",
        )
        avatar.pack(side="left", padx=(0, 10))
        make_label(
            owner,
            f"Discord user: {row.get('operator') or 'Unknown'}",
            font=("Segoe UI", 10, "normal"),
            background=palette["surfaceContainerLow"],
        ).pack(side="left", anchor="center")

        profile_details = str(row.get("details_text") or "").strip()
        if profile_details:
            make_label(
                profile,
                profile_details,
                font=("Segoe UI", 10, "normal"),
                background=palette["surfaceContainerLow"],
                wraplength=820,
                justify="left",
            ).pack(anchor="w", pady=(16, 2))

        page_layout = row.get("page_layout")
        if isinstance(page_layout, dict) and page_layout.get("blocks"):
            layout_card = make_card(page, padding=12)
            layout_card.pack(fill="x", pady=(0, 15))
            make_label(
                layout_card,
                "Station page",
                font=("Segoe UI", 15, "bold"),
                background=palette["surfaceContainerLow"],
            ).pack(anchor="w", pady=(0, 10))
            layout_height = page_layout.get("height", 420)
            layout_height = max(320, min(1_200, int(layout_height)))
            layout = tk.Frame(
                layout_card,
                bg=palette["surfaceContainerLowest"],
                height=layout_height,
            )
            layout.pack(fill="x")
            layout.pack_propagate(False)
            for block in page_layout["blocks"]:
                block_type = block.get("type", "text")
                x = max(0.0, min(100.0, float(block.get("x", 8))))
                y = max(0.0, min(100.0, float(block.get("y", 8))))
                width = max(5.0, min(100.0, float(block.get("width", 84))))
                height = max(8.0, min(900.0, float(block.get("height", 72))))
                if block_type == "separator":
                    tk.Frame(layout, bg=palette["outlineVariant"], height=2).place(
                        relx=x / 100, rely=y / 100, relwidth=width / 100, height=2
                    )
                elif block_type == "image" and block.get("image_url"):
                    image_block = make_image_label(
                        layout,
                        str(block.get("image_url")),
                        size=min(360, max(80, int(width * 5))),
                        placeholder="Image",
                        role=f"station page image: {row.get('station', 'unknown')}",
                    )
                    image_block.place(
                        relx=x / 100,
                        rely=y / 100,
                        relwidth=width / 100,
                        height=min(300, height),
                    )
                else:
                    font_name = str(block.get("font") or "Segoe UI")
                    if font_name not in {"Lexend", "Segoe UI", "Georgia", "Courier New", "Impact", "system-ui"}:
                        font_name = "Segoe UI"
                    font_size = int(block.get("font_size", 18))
                    font_weight = int(block.get("font_weight", 400))
                    font_style = "bold" if font_weight >= 600 else "normal"
                    text_align = str(block.get("align") or "left")
                    text_widget = make_label(
                        layout,
                        str(block.get("text") or ""),
                        font=(font_name if font_name != "system-ui" else "Segoe UI", font_size, font_style),
                        background=palette["surfaceContainerLowest"],
                        justify=text_align,
                        anchor="center" if text_align == "center" else ("e" if text_align == "right" else "w"),
                        wraplength=620,
                    )
                    text_widget.place(
                        relx=x / 100,
                        rely=y / 100,
                        relwidth=width / 100,
                        height=min(300, height),
                    )
        else:
            details_text = profile_details or "No extra station details yet."
            details_card = make_card(page, padding=16)
            details_card.pack(fill="x", pady=(0, 12))
            make_label(
                details_card,
                "Station details",
                font=("Segoe UI", 15, "bold"),
                background=palette["surfaceContainerLow"],
            ).pack(anchor="w", pady=(0, 8))
            make_label(
                details_card,
                details_text,
                font=("Segoe UI", 10, "normal"),
                color=palette["onSurfaceVariant"],
                background=palette["surfaceContainerLow"],
                wraplength=840,
            ).pack(anchor="w")

        gallery_urls = [
            url for url in row.get("image_urls", [])
            if isinstance(url, str) and url and url != row.get("logo_url")
        ]
        if gallery_urls:
            gallery_card = make_card(page, padding=16)
            gallery_card.pack(fill="x", pady=(0, 12))
            make_label(
                gallery_card,
                "Images",
                font=("Segoe UI", 15, "bold"),
                background=palette["surfaceContainerLow"],
            ).pack(anchor="w", pady=(0, 10))
            gallery = tk.Frame(gallery_card, bg=palette["surfaceContainerLow"])
            gallery.pack(anchor="w")
            for url in gallery_urls[:6]:
                make_image_label(gallery, url, size=140, placeholder="Image", role=f"station gallery: {row.get('station', 'unknown')}").pack(
                    side="left",
                    padx=(0, 10),
                )

        comments = row.get("comments") if isinstance(row.get("comments"), list) else []
        comments_card = make_card(page, padding=16)
        comments_card.pack(fill="x", pady=(0, 16))
        make_label(
            comments_card,
            f"Comments ({row.get('comment_count', len(comments))})",
            font=("Segoe UI", 16, "bold"),
            background=palette["surfaceContainerLow"],
        ).pack(anchor="w", pady=(0, 12))
        if not comments:
            make_label(
                comments_card,
                "No comments have been posted.",
                color=palette["onSurfaceVariant"],
                background=palette["surfaceContainerLow"],
            ).pack(anchor="w")
        else:
            for comment in comments[:30]:
                comment_row = tk.Frame(comments_card, bg=palette["surfaceContainerLow"])
                comment_row.pack(fill="x", pady=(0, 12))
                comment_avatar = make_image_label(
                    comment_row,
                    str(comment.get("avatar_url") or ""),
                    size=40,
                    placeholder="D",
                    role=f"comment avatar: {comment.get('author', 'unknown')}",
                )
                comment_avatar.pack(side="left", padx=(0, 10))
                comment_body = tk.Frame(comment_row, bg=palette["surfaceContainerLow"])
                comment_body.pack(side="left", fill="x", expand=True)
                comment_title = str(comment.get("author") or "TXITY user")
                timestamp = str(comment.get("timestamp") or "")
                make_label(
                    comment_body,
                    f"{comment_title}  {timestamp}".strip(),
                    font=("Segoe UI", 9, "bold"),
                    background=palette["surfaceContainerLow"],
                ).pack(anchor="w")
                make_label(
                    comment_body,
                    str(comment.get("text") or ""),
                    font=("Segoe UI", 9, "normal"),
                    color=palette["onSurfaceVariant"],
                    background=palette["surfaceContainerLow"],
                    wraplength=790,
                ).pack(anchor="w", pady=(3, 0))

    def show_discord(parent: tk.Frame) -> None:
        page = tk.Frame(parent, bg=palette["background"])
        page.pack(fill="both", expand=True)

        make_label(
            page,
            "Discord account",
            font=("Segoe UI", 25, "bold"),
            background=palette["background"],
        ).pack(anchor="w", pady=(8, 18))

        card = make_card(page, padding=16)
        card.pack(fill="x")

        make_label(
            card,
            variable=account_var,
            font=("Segoe UI", 15, "bold"),
            background=palette["surfaceContainerLow"],
        ).pack(anchor="w")

        make_label(
            card,
            variable=auth_status_var,
            font=("Segoe UI", 10, "normal"),
            color=palette["onSurfaceVariant"],
            background=palette["surfaceContainerLow"],
            wraplength=780,
        ).pack(anchor="w", pady=(10, 18))

        buttons = tk.Frame(card, bg=palette["surfaceContainerLow"])
        buttons.pack(anchor="w")
        make_button(
            buttons,
            "Sign in with Discord",
            on_sign_in,
            kind="primary",
        ).pack(side="left", padx=(0, 8))
        make_button(
            buttons,
            "Disconnect Discord",
            disconnect_discord,
            kind="tonal",
        ).pack(side="left")

        description = (
            "TXITPY uses TXITY's official Discord sign-in flow. Your website session "
            "is kept in a separate browser profile and checked automatically when the "
            "desktop app starts. The active session token remains in memory."
        )
        make_label(
            card,
            description,
            font=("Segoe UI", 9, "normal"),
            color=palette["onSurfaceVariant"],
            background=palette["surfaceContainerLow"],
            wraplength=780,
        ).pack(anchor="w", pady=(22, 0))

        make_label(
            page,
            f"Server: {BASE_URL}",
            font=("Segoe UI", 9, "normal"),
            color=palette["primary"],
            background=palette["background"],
        ).pack(anchor="w", pady=(18, 0))

    def build_shell() -> None:
        nonlocal shell_frame, content_frame, nav_frame, station_cards_frame, tx_transmit_button

        nonlocal active_scroll_canvas
        active_scroll_canvas = None
        for child in root.winfo_children():
            child.destroy()

        station_cards_frame = None
        nav_frame = None
        tx_transmit_button = None
        shell_frame = tk.Frame(root, bg=palette["background"])
        shell_frame.pack(fill="both", expand=True)

        topbar = SurfaceFrame(
            shell_frame,
            background=palette["surfaceContainerLow"],
            outside=palette["background"],
            outline=palette["outlineVariant"],
            padding=10,
            padding_x=28,
            padding_y=8,
        )
        topbar.configure(height=64)
        topbar.pack(fill="x", padx=18, pady=(12, 10))
        topbar.pack_propagate(False)

        make_label(
            topbar,
            "TXITY",
            font=("Segoe UI", 17, "bold"),
            color=palette["primary"],
            background=palette["surfaceContainerLow"],
        ).pack(side="left", padx=(2, 12))

        if discord_authenticated:
            navigation = tk.Frame(topbar, bg=palette["surfaceContainerLow"])
            navigation.pack(side="left", fill="y")

            destinations = (
                ("Transmit", "transmit"),
                ("Overview", "overview"),
                ("Stations", "stations"),
                ("Discord", "discord"),
            )
            for label, destination in destinations:
                selected = (
                    current_page == destination
                    or (destination == "stations" and current_page == "station_detail")
                )
                nav_button = FlatButton(
                    navigation,
                    label,
                    lambda target=destination: navigate(target),
                    background=(
                        palette["primaryContainer"]
                        if selected else palette["surfaceContainerLow"]
                    ),
                    foreground=(
                        palette["onPrimaryContainer"]
                        if selected else palette["onSurfaceVariant"]
                    ),
                    hover_background=(
                        palette["primaryContainer"]
                        if selected else palette["surfaceContainerHigh"]
                    ),
                    hover_foreground=palette["onPrimaryContainer"],
                    outside=palette["surfaceContainerLow"],
                    outline=(
                        palette["primaryContainer"]
                        if selected else palette["surfaceContainerLow"]
                    ),
                    focus_color=palette["primary"],
                    font=("Segoe UI", 10, "bold" if selected else "normal"),
                    padding_x=14,
                    padding_y=9,
                )
                nav_button.pack(side="left", padx=2, pady=2)

            make_button(
                topbar,
                "Open TXITY",
                lambda: webbrowser.open(BASE_URL),
                kind="text",
                padx=10,
                pady=8,
            ).pack(side="left", padx=(8, 0))

            make_button(
                topbar,
                "Light mode" if theme_mode == "dark" else "Dark mode",
                toggle_theme,
                kind="tonal",
                padx=12,
                pady=7,
            ).pack(side="right", padx=(8, 2))

            make_label(
                topbar,
                variable=connection_var,
                font=("Segoe UI", 8, "normal"),
                color=palette["onSurfaceVariant"],
                background=palette["surfaceContainerLow"],
            ).pack(side="right", padx=(6, 8))

            content_frame = tk.Frame(
                shell_frame,
                bg=palette["background"],
                padx=20,
                pady=10,
            )
            content_frame.pack(fill="both", expand=True)

            if current_page == "transmit":
                show_transmit(content_frame)
            elif current_page == "stations":
                show_stations(content_frame)
            elif current_page == "station_detail":
                show_station_detail(content_frame)
            elif current_page == "discord":
                show_discord(content_frame)
            else:
                show_overview(content_frame)
        else:
            make_button(
                topbar,
                "Light mode" if theme_mode == "dark" else "Dark mode",
                toggle_theme,
                kind="tonal",
                padx=12,
                pady=7,
            ).pack(side="right")
            content_frame = tk.Frame(
                shell_frame,
                bg=palette["background"],
                padx=20,
                pady=16,
            )
            content_frame.pack(fill="both", expand=True)
            show_gate(content_frame)

    def apply_users_metrics(metrics: dict[str, Any]) -> None:
        nonlocal users_listing_received
        users_listing_received = True

        def metric_text(name: str) -> str:
            value = metrics.get(name)
            return str(value) if isinstance(value, int) else "Not reported"

        web_count = metric_text("webClients")
        sdrpp_count = metric_text("sdrppClients")
        clients_var.set(
            f"{web_count} web / {sdrpp_count} SDR++"
            if web_count != "Not reported" and sdrpp_count != "Not reported"
            else "Not reported"
        )
        transmitters_var.set(metric_text("transmitting"))
        sessions_var.set(metric_text("sessions"))
        web_audio_var.set(metric_text("webAudioClients"))
        sdrpp_streaming_var.set(metric_text("sdrppStreamingClients"))

        timestamp = metrics.get("updatedAtLocal")
        updated_var.set(
            f"TXITY users listing updated at {timestamp}"
            if isinstance(timestamp, str) and timestamp
            else "TXITY users listing refreshed"
        )

    def apply_status(data: dict[str, Any]) -> None:
        nonlocal server_band_min_hz, server_band_max_hz
        nonlocal tx_negative_status_reason, tx_negative_status_count
        center_var.set(format_frequency(data.get("centerHz")))
        low, high = data.get("bandMinHz"), data.get("bandMaxHz")
        if isinstance(low, (int, float)) and isinstance(high, (int, float)):
            server_band_min_hz = int(low)
            server_band_max_hz = int(high)
            band_var.set(f"{format_frequency(low)} - {format_frequency(high)}")
        else:
            server_band_min_hz = None
            server_band_max_hz = None
        update_frequency_check()
        rate = data.get("sampleRate")
        sample_rate_var.set(
            f"{rate / 1_000_000:.3f} MS/s"
            if isinstance(rate, (int, float)) else "Not reported"
        )
        if not users_listing_received:
            transmitters_var.set(str(data.get("transmittingCount", "Not reported")))
            web_clients, sdrpp_clients = data.get("webClients"), data.get("sdrppClients")
            if isinstance(web_clients, int) and isinstance(sdrpp_clients, int):
                clients_var.set(f"{web_clients} web / {sdrpp_clients} SDR++")
        protection = "enabled" if data.get("protection") else "not reported"
        summary_var.set(
            f"Server protection: {protection}. "
            f"TXITY reports {data.get('transmittingCount', 'an unknown number')} active transmitters."
        )

        if transmitter.is_busy and data.get("ptt") is True:
            tx_negative_status_reason = None
            tx_negative_status_count = 0
            tx_status_var.set("TXITY confirms that the station is transmitting.")
        elif transmitter.is_busy and data.get("ptt") is False:
            queue_position = data.get("queuePosition")
            if isinstance(queue_position, int) and queue_position >= 0:
                tx_negative_status_reason = None
                tx_negative_status_count = 0
                tx_status_var.set(f"Waiting in the TXITY transmitter queue (position {queue_position + 1}).")
            elif data.get("txStartUsable") is False or data.get("txBlockReason") not in (None, False, ""):
                reason = data.get("txStartReason", data.get("txBlockReason"))
                reasons = {
                    2: "transmitter capacity is currently full",
                    3: "frequency sharing must be enabled by both stations",
                    4: "the total transmitter limit has been reached",
                    5: "a station name is required",
                    "2": "transmitter capacity is currently full",
                    "3": "frequency sharing must be enabled by both stations",
                    "4": "the total transmitter limit has been reached",
                    "5": "a station name is required",
                }
                reason_text = reasons.get(reason, "TXITY did not accept the transmission request")
                reason_key = str(reason if reason is not None else "txStartUsable=false")
                if not transmitter.server_rejection_ready:
                    tx_status_var.set("Waiting for TXITY to confirm the transmission request...")
                else:
                    if reason_key == tx_negative_status_reason:
                        tx_negative_status_count += 1
                    else:
                        tx_negative_status_reason = reason_key
                        tx_negative_status_count = 1
                    if tx_negative_status_count >= 2:
                        message = f"Transmission blocked by TXITY: {reason_text}."
                        tx_status_var.set(message)
                        print(f"[TXITPY] {message}", file=sys.stderr, flush=True)
                        transmitter.stop()
            else:
                tx_negative_status_reason = None
                tx_negative_status_count = 0

    def drain_events() -> None:
        nonlocal discord_authenticated, auth_restore_started, auth_enabled, station_rows, current_page, tx_profile_initialized
        while True:
            try:
                event = client.events.get_nowait()
            except queue.Empty:
                break

            kind = event.get("kind")
            if kind == "connection":
                text = str(event.get("text", ""))
                connection_var.set(text)
                print(f"[TXITPY] {text}", file=sys.stderr, flush=True)

            elif kind == "status":
                apply_status(event.get("data", {}))
                updated_var.set("Live status received")

            elif kind == "tx_state":
                state = str(event.get("state") or "")
                tx_status_var.set(str(event.get("text") or state))
                update_transmit_button()
                print(f"[TXITPY] Transmission state: {state}.", file=sys.stderr, flush=True)

            elif kind == "tx_error":
                details = safe_error_text(str(event.get("text") or "Transmission failed."), 800)
                tx_status_var.set(f"Transmission error: {details}")
                print(f"[TXITPY] Transmission error: {details}", file=sys.stderr, flush=True)
                update_transmit_button()

            elif kind == "tx_log":
                details = safe_error_text(str(event.get("text") or ""), 500)
                if details:
                    print(f"[TXITPY] {details}", file=sys.stderr, flush=True)

            elif kind == "tx_audio_warning":
                details = safe_error_text(str(event.get("text") or ""), 300)
                if details:
                    print(f"[TXITPY] Audio input warning: {details}", file=sys.stderr, flush=True)

            elif kind == "tx_server_message":
                data = event.get("data", {})
                if isinstance(data, dict):
                    message_type = str(data.get("type") or "")
                    server_text = safe_error_text(
                        str(data.get("message") or data.get("reason") or message_type),
                        500,
                    )
                    tx_status_var.set(f"TXITY: {server_text}")
                    print(f"[TXITPY] TXITY {message_type}: {server_text}", file=sys.stderr, flush=True)
                    if message_type in {"kicked", "banned"}:
                        transmitter.stop()

            elif kind == "tx_metrics":
                tx_metrics_var.set(
                    f"Audio frames sent: {event.get('frames_sent', 0)}"
                    f" · dropped: {event.get('frames_dropped', 0)}"
                    f" · queued: {event.get('queue_depth', 0)}"
                )

            elif kind == "config":
                config = event.get("data", {})
                if isinstance(config, dict):
                    if center_var.get() == "Not reported":
                        center_var.set(format_frequency(config.get("centerHz")))

                    auth_manager.configure(
                        config.get("discordAuthUrl"),
                        config.get("secureDiscordAuth") is True,
                    )
                    auth_enabled = config.get("secureDiscordAuth") is True
                    if auth_enabled and not auth_restore_started:
                        auth_restore_started = True
                        account_var.set("Checking saved Discord login...")
                        auth_status_var.set("Checking the saved TXITY browser session.")
                        auth_manager.restore_session()
                    elif not auth_enabled:
                        auth_status_var.set(
                            "TXITY secure Discord sign-in is not enabled. The dashboard remains locked."
                        )

            elif kind == "users_listing":
                station_rows = event.get("rows", [])
                update_frequency_check()
                if not tx_profile_initialized and client.discord_user:
                    own_id = str(client.discord_user.get("id") or "")
                    own_station = next(
                        (row for row in station_rows if str(row.get("discord_id") or "") == own_id),
                        None,
                    )
                    if own_station:
                        tx_station_name_var.set(str(own_station.get("station") or "")[:48])
                        tx_description_var.set(str(own_station.get("description") or "")[:180])
                        tx_logo_url_var.set(str(own_station.get("logo_url") or "")[:512])
                        tx_details_var.set(str(own_station.get("details_text") or "")[:1_200])
                        tx_page_layout_var.set(str(own_station.get("page_layout_json") or "")[:40_000])
                    elif not tx_station_name_var.get().strip():
                        display = (
                            client.discord_user.get("globalName")
                            or client.discord_user.get("username")
                            or ""
                        )
                        tx_station_name_var.set(str(display)[:48])
                    tx_profile_initialized = True
                image_cache.clear()
                image_loading.clear()
                image_labels.clear()
                apply_users_metrics(event.get("metrics", {}))
                station_status_var.set(
                    f"Loaded {len(station_rows)} stations from TXITY users.json."
                    if station_rows else
                    "TXITY users.json loaded successfully, but its stations array is empty."
                )
                for station in station_rows:
                    station_name = safe_error_text(str(station.get("station") or "Unknown station"), 100)
                    description = str(station.get("description") or "").strip()
                    source = str(station.get("description_source") or "none")
                    if description:
                        print(
                            f"[TXITPY] Description loaded: {station_name} "
                            f"(source={source}, {len(description)} characters).",
                            file=sys.stderr,
                            flush=True,
                        )
                    else:
                        print(
                            f"[TXITPY] Description missing: {station_name} "
                            "(shortDescription, description, summary, details and pageLayout text were empty).",
                            file=sys.stderr,
                            flush=True,
                        )
                    profile_details = str(station.get("details_text") or "").strip()
                    if profile_details:
                        print(
                            f"[TXITPY] Station details loaded: {station_name} "
                            f"({len(profile_details)} characters).",
                            file=sys.stderr,
                            flush=True,
                        )
                    else:
                        print(
                            f"[TXITPY] Station details empty: {station_name}.",
                            file=sys.stderr,
                            flush=True,
                        )
                update_station_tree()
                summary_var.set(
                    f"TXITY currently reports "
                    f"{event.get('metrics', {}).get('transmitting', 'an unknown number')} transmitting stations."
                )
                print(
                    f"[TXITPY] Loaded {len(station_rows)} stations from users.json.",
                    file=sys.stderr,
                    flush=True,
                )

            elif kind == "station_image":
                url = event.get("url")
                data = event.get("data")
                if isinstance(url, str) and isinstance(data, bytes):
                    apply_station_image(url, data)

            elif kind == "station_image_error":
                url = event.get("url")
                if isinstance(url, str):
                    image_loading.discard(url)
                    waiting = image_labels.pop(url, [])
                    roles = sorted({role for _label, _size, role in waiting})
                    role_text = ", ".join(roles) if roles else "no active image widgets"
                    print(
                        f"[TXITPY] Image load error: {role_text} "
                        f"({describe_media_url(url)}): "
                        f"{safe_error_text(str(event.get('text') or 'Unknown image error'), 500)}",
                        file=sys.stderr,
                        flush=True,
                    )

            elif kind == "stations_error":
                details = safe_error_text(
                    str(event.get("text", "Station refresh failed.")),
                    max_chars=1_200,
                )
                station_status_var.set(details)
                summary_var.set(details)
                print(f"[TXITPY] {details}", file=sys.stderr, flush=True)

            elif kind == "auth_token":
                tx_profile_initialized = False
                token = event.get("token")
                user = clean_discord_user(event.get("user"))
                if is_auth_token(token):
                    client.set_discord_auth(token, user)
                    discord_authenticated = True
                    display_name = (
                        (user or {}).get("globalName")
                        or (user or {}).get("username")
                        or "Discord account"
                    )
                    account_var.set(f"Signed in as {display_name}")
                    auth_status_var.set("Discord sign-in completed.")
                    station_status_var.set("Loading station data from TXITY...")
                    current_page = "overview"
                    build_shell()
                    client.connect()
                    print(
                        "[TXITPY] Discord session attached; connecting to the status feed.",
                        file=sys.stderr,
                        flush=True,
                    )
                else:
                    auth_status_var.set("TXITY returned an invalid Discord session.")

            elif kind == "auth_restore_missing":
                account_var.set("Discord sign-in required")
                auth_status_var.set("No saved Discord session was found. Sign in to continue.")
                build_shell()

            elif kind == "auth_signed_out":
                transmitter.stop(wait=0.5)
                tx_profile_initialized = False
                client.set_discord_auth(None, None)
                discord_authenticated = False
                account_var.set("Not signed in to Discord")
                auth_status_var.set("TXITY's Discord session was disconnected.")
                build_shell()

            elif kind == "auth_browser":
                auth_status_var.set(str(event.get("text", "")))

            elif kind == "auth_error":
                details = safe_error_text(
                    str(event.get("text", "Sign-in browser error")),
                    max_chars=16_000,
                )
                print(
                    f"\n[TXITPY Discord sign-in error]\n{details}\n",
                    file=sys.stderr,
                    flush=True,
                )
                auth_status_var.set(details)

            elif kind == "socket_error":
                details = safe_error_text(
                    str(event.get("text", "Unknown status connection error")),
                    max_chars=1_500,
                )
                connection_var.set("Live status unavailable")
                summary_var.set(details)
                print(f"[TXITPY] Status WebSocket error: {details}", file=sys.stderr, flush=True)

            elif kind == "error":
                details = safe_error_text(
                    str(event.get("text", "Network error")),
                    max_chars=1_500,
                )
                summary_var.set(details)
                if not auth_enabled:
                    auth_status_var.set(details)

        root.after(250, drain_events)

    def periodic_station_refresh() -> None:
        if discord_authenticated:
            client.refresh_stations()
        root.after(60_000, periodic_station_refresh)

    build_shell()
    root.protocol("WM_DELETE_WINDOW", on_shutdown)
    root.after(250, drain_events)
    root.after(60_000, periodic_station_refresh)
    client.load_public_data()
    root.mainloop()


if __name__ == "__main__":
    try:
        build_app()
    except Exception as exc:
        details = format_exception_details(exc)
        log_path = write_diagnostic(
            "main-window-startup",
            exc,
            filename="application-errors.log",
        )
        message = (
            "\n[TXITPY main window startup error]\n"
            f"Exception: {type(exc).__name__}: {safe_error_text(exc)}\n\n"
            f"Traceback:\n{details}"
        )
        if log_path is not None:
            message += f"\n\nFull diagnostic log: {log_path}"
        print(message, file=sys.stderr, flush=True)
