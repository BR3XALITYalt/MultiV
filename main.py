"""TXITPY desktop client for TXITY's station directory, status feed, and virtual transmitter.

Discord sign-in uses TXITY's official OAuth route and a one-use loopback callback.
"""
from __future__ import annotations

import json
import os
import queue
import re
import secrets
import sys
import threading
import uuid
import webbrowser
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

from audio_playlist import (
    build_playlist_snapshot,
    normalize_audio_sources,
)
from discord_auth import (
    DiscordAuthManager,
    clean_discord_user,
    format_exception_details,
    is_auth_token,
    safe_error_text,
    write_diagnostic,
    write_diagnostic_text,
)
from drm_support import (
    DRM_LANGUAGES,
    DRM_PROGRAMME_TYPES,
    DRM_AUDIO_CODECS,
    DRM_MSC_MODES,
    drm_setup_status,
    prepare_drm_encoder,
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


def tx_settings_file() -> Path:
    """Return the per-user settings file path without storing account credentials."""
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / "TXITPY" / "transmit-settings.json"


def load_tx_settings() -> dict[str, Any]:
    """Load local station and transmit preferences, ignoring invalid files safely."""
    path = tx_settings_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        return {}
    return data if isinstance(data, dict) else {}


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
        self.control_generation = 0

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
            with self.auth_lock:
                self.control_generation += 1
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
                ws: websocket.WebSocketApp | None = None
                try:
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
                except Exception as exc:
                    if not self.stop_event.is_set():
                        details = describe_status_socket_error(exc)
                        write_diagnostic_text("status-websocket-client-error", details)
                        self.events.put({"kind": "socket_error", "text": details})
                finally:
                    if ws is not None:
                        with self.auth_lock:
                            if self.ws is ws:
                                self.ws = None

                if not self.stop_event.is_set():
                    self.events.put({
                        "kind": "connection",
                        "text": "TXITY control connection lost; reconnecting...",
                    })
                    if self.reconnect_event.wait(4):
                        self.reconnect_event.clear()
        finally:
            with self.auth_lock:
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
    from tkinter import ttk, filedialog, messagebox
    from material_theme import apply_tk_theme, native_widget_classes

    SurfaceFrame, FlatButton = native_widget_classes(tk)

    client = TxityClient()
    auth_manager = DiscordAuthManager(client.events)
    transmitter = TxityTransmitter(client, client.events)

    theme_mode = _system_theme_mode()

    root = tk.Tk()
    palette = apply_tk_theme(root, theme_mode)
    root.title("TXITPY")
    root.geometry("1180x790")
    root.minsize(900, 650)
    root.configure(background=palette["background"])

    tray_icon: Any = None
    tray_setup_ready = threading.Event()

    saved_tx_settings = load_tx_settings()

    def saved_text(key: str, default: str) -> str:
        value = saved_tx_settings.get(key, default)
        return str(value) if value is not None else default

    def saved_bool(key: str, default: bool) -> bool:
        value = saved_tx_settings.get(key, default)
        return value if isinstance(value, bool) else default

    fade_shape_names = {
        "0": "Smooth QSB dip",
        "1": "Linear triangle dip",
        "2": "Deep notch fade",
        "3": "Fast flutter / multipath",
        "4": "Rolling multi-rate QSB",
        "5": "Random QSB",
    }
    saved_fade_shape_id = saved_text("fade_shape", "0").split("-", 1)[0].strip()
    if saved_fade_shape_id not in fade_shape_names:
        saved_fade_shape_id = "0"
    fade_shape_saved_value = f"{saved_fade_shape_id} - {fade_shape_names[saved_fade_shape_id]}"

    # Tk variables live for the lifetime of the root, even when page widgets
    # are rebuilt after navigation or a theme change.
    connection_var = tk.StringVar(root, "Disconnected")
    account_var = tk.StringVar(root, "Not signed in to Discord")
    auth_status_var = tk.StringVar(root, "Checking TXITY server configuration...")
    station_status_var = tk.StringVar(root, "Waiting for TXITY station data.")
    station_count_var = tk.StringVar(root, "0 stations")

    tx_station_name_var = tk.StringVar(root, saved_text("station_name", ""))
    tx_description_var = tk.StringVar(root, saved_text("description", ""))
    tx_frequency_var = tk.StringVar(root, saved_text("frequency", "7.250 MHz"))
    tx_frequency_check_var = tk.StringVar(root, "Waiting for TXITY station data.")
    tx_mode_var = tk.StringVar(root, saved_text("mode", "AM"))
    tx_mode_help_var = tk.StringVar(root, "AM uses a carrier with upper and lower sidebands.")
    tx_drm_service_label_var = tk.StringVar(root, saved_text("drm_service_label", ""))
    tx_drm_service_id_var = tk.StringVar(root, saved_text("drm_service_id", "0xB30001"))
    tx_drm_language_var = tk.StringVar(root, saved_text("drm_language", "English"))
    tx_drm_programme_type_var = tk.StringVar(root, saved_text("drm_programme_type", "No programme type"))
    tx_drm_iso_language_var = tk.StringVar(root, saved_text("drm_iso_language", ""))
    tx_drm_iso_country_var = tk.StringVar(root, saved_text("drm_iso_country", ""))
    tx_drm_audio_codec_var = tk.StringVar(root, saved_text("drm_audio_codec", "xHE-AAC"))
    tx_drm_msc_mode_var = tk.StringVar(root, saved_text("drm_msc_mode", "16-QAM"))
    tx_drm_interleaving_var = tk.StringVar(root, saved_text("drm_interleaving", "Long (2 s)"))
    drm_setup_state: dict[str, Any] = {
        "running": False,
        "previous_mode": (
            tx_mode_var.get().strip().upper()
            if tx_mode_var.get().strip().upper() not in {"", "DRM"}
            else "AM"
        ),
    }
    drm_setup_events: queue.Queue[tuple[str, str]] = queue.Queue()
    tx_power_var = tk.StringVar(root, saved_text("station_dbm", "50"))
    tx_power_readout_var = tk.StringVar(root, "50 dBm")
    tx_audio_source_var = tk.StringVar(root, saved_text("audio_source", "Microphone / line input"))
    tx_audio_device_var = tk.StringVar(root, saved_text("audio_device", "System default"))
    tx_audio_device_label_var = tk.StringVar(root, "Input device")
    tx_audio_input_var = tx_audio_device_var
    saved_playlist_files = saved_tx_settings.get("playlist_files", [])
    saved_playlist_sources = saved_tx_settings.get("playlist_sources")
    if not isinstance(saved_playlist_sources, list):
        saved_playlist_sources = saved_playlist_files
    tx_playlist_sources = normalize_audio_sources(
        saved_playlist_sources if isinstance(saved_playlist_sources, list) else []
    )
    saved_playlist_exclusions = saved_tx_settings.get("playlist_excluded_files", [])
    tx_playlist_excluded_files = {
        path.casefold()
        for path in normalize_audio_sources(
            saved_playlist_exclusions if isinstance(saved_playlist_exclusions, list) else []
        )
    }
    tx_playlist_files, tx_playlist_signature = build_playlist_snapshot(
        tx_playlist_sources,
        tx_playlist_excluded_files,
    )
    playlist_watch_lock = threading.Lock()
    playlist_watch_revision = 0
    playlist_watch_signature = tx_playlist_signature
    playlist_watch_stop = threading.Event()
    playlist_ui_refresh_callback: Any = None
    tx_playlist_shuffle_var = tk.BooleanVar(root, saved_bool("playlist_shuffle", False))
    tx_playlist_loop_var = tk.BooleanVar(root, saved_bool("playlist_loop", False))
    tx_playlist_status_var = tk.StringVar(
        root,
        (
            f"{len(tx_playlist_files)} track{'s' if len(tx_playlist_files) != 1 else ''} loaded."
            if tx_playlist_files else "No playlist loaded."
        ),
    )
    tx_cw_text_var = tk.StringVar(root, saved_text("cw_text", "CQ CQ DE TXITPY"))
    tx_cw_wpm_var = tk.StringVar(root, saved_text("cw_wpm", "18"))
    tx_cw_loop_var = tk.BooleanVar(root, saved_bool("cw_loop", True))
    tx_allow_overlap_var = tk.BooleanVar(root, saved_bool("allow_overlap", False))
    tx_logo_url_var = tk.StringVar(root, saved_text("station_logo_url", ""))
    tx_details_var = tk.StringVar(root, saved_text("station_details", ""))
    tx_page_layout_var = tk.StringVar(root, saved_text("station_page_layout", ""))

    tx_fade_enabled_var = tk.BooleanVar(root, saved_bool("fade_enabled", False))
    tx_fade_seconds_var = tk.StringVar(root, saved_text("fade_seconds", "15"))
    tx_fade_min_dbm_var = tk.StringVar(root, saved_text("fade_min_dbm", "25"))
    tx_fade_shape_var = tk.StringVar(root, fade_shape_saved_value)
    tx_agc_enabled_var = tk.BooleanVar(root, saved_bool("agc_enabled", True))
    tx_agc_target_var = tk.StringVar(root, saved_text("agc_target", "0.19"))
    tx_mic_gain_var = tk.StringVar(root, saved_text("mic_gain_db", "0"))
    tx_bass_var = tk.StringVar(root, saved_text("bass_db", "0"))
    tx_treble_var = tk.StringVar(root, saved_text("treble_db", "0"))
    tx_presence_var = tk.StringVar(root, saved_text("presence_db", "0"))
    tx_drive_var = tk.StringVar(root, saved_text("drive", "0.015"))
    tx_ssb_bandwidth_var = tk.StringVar(root, saved_text("ssb_bandwidth_hz", "10000"))
    tx_ssb_low_cut_var = tk.StringVar(root, saved_text("ssb_low_cut_hz", "325"))
    tx_ssb_high_cut_var = tk.StringVar(root, saved_text("ssb_high_cut_hz", "0"))
    tx_am_bandwidth_var = tk.StringVar(root, saved_text("am_bandwidth_hz", "12000"))
    tx_am_pre_emphasis_var = tk.BooleanVar(root, saved_bool("am_pre_emphasis", False))
    tx_status_var = tk.StringVar(root, "Not transmitting")
    tx_audio_device_ids: dict[str, int | None] = {"System default": None}
    tx_profile_initialized = bool(saved_text("station_name", "").strip())

    settings_save_after_id: str | None = None

    def tx_settings_snapshot() -> dict[str, Any]:
        return {
            "station_name": tx_station_name_var.get(),
            "description": tx_description_var.get(),
            "frequency": tx_frequency_var.get(),
            "mode": tx_mode_var.get(),
            "drm_service_label": tx_drm_service_label_var.get(),
            "drm_service_id": tx_drm_service_id_var.get(),
            "drm_language": tx_drm_language_var.get(),
            "drm_programme_type": tx_drm_programme_type_var.get(),
            "drm_iso_language": tx_drm_iso_language_var.get(),
            "drm_iso_country": tx_drm_iso_country_var.get(),
            "drm_audio_codec": tx_drm_audio_codec_var.get(),
            "drm_msc_mode": tx_drm_msc_mode_var.get(),
            "drm_interleaving": tx_drm_interleaving_var.get(),
            "station_dbm": tx_power_var.get(),
            "audio_source": tx_audio_source_var.get(),
            "audio_device": tx_audio_device_var.get(),
            "playlist_files": list(tx_playlist_files),
            "playlist_sources": list(tx_playlist_sources),
            "playlist_excluded_files": sorted(tx_playlist_excluded_files),
            "playlist_shuffle": tx_playlist_shuffle_var.get(),
            "playlist_loop": tx_playlist_loop_var.get(),
            "cw_text": tx_cw_text_var.get(),
            "cw_wpm": tx_cw_wpm_var.get(),
            "cw_loop": tx_cw_loop_var.get(),
            "allow_overlap": tx_allow_overlap_var.get(),
            "station_logo_url": tx_logo_url_var.get(),
            "station_details": tx_details_var.get(),
            "station_page_layout": tx_page_layout_var.get(),
            "fade_enabled": tx_fade_enabled_var.get(),
            "fade_seconds": tx_fade_seconds_var.get(),
            "fade_min_dbm": tx_fade_min_dbm_var.get(),
            "fade_shape": tx_fade_shape_var.get(),
            "agc_enabled": tx_agc_enabled_var.get(),
            "agc_target": tx_agc_target_var.get(),
            "mic_gain_db": tx_mic_gain_var.get(),
            "bass_db": tx_bass_var.get(),
            "treble_db": tx_treble_var.get(),
            "presence_db": tx_presence_var.get(),
            "drive": tx_drive_var.get(),
            "ssb_bandwidth_hz": tx_ssb_bandwidth_var.get(),
            "ssb_low_cut_hz": tx_ssb_low_cut_var.get(),
            "ssb_high_cut_hz": tx_ssb_high_cut_var.get(),
            "am_bandwidth_hz": tx_am_bandwidth_var.get(),
            "am_pre_emphasis": tx_am_pre_emphasis_var.get(),
        }

    def save_tx_settings_now() -> None:
        nonlocal settings_save_after_id
        if settings_save_after_id is not None:
            try:
                root.after_cancel(settings_save_after_id)
            except tk.TclError:
                pass
            settings_save_after_id = None
        path = tx_settings_file()
        temporary = path.with_suffix(path.suffix + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(tx_settings_snapshot(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(path)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def schedule_tx_settings_save(*_args) -> None:
        nonlocal settings_save_after_id
        if settings_save_after_id is not None:
            try:
                root.after_cancel(settings_save_after_id)
            except tk.TclError:
                pass
        settings_save_after_id = root.after(450, save_tx_settings_now)

    def refresh_playlist_from_sources(*, save: bool = True) -> tuple[int, int]:
        nonlocal playlist_watch_revision, playlist_watch_signature
        with playlist_watch_lock:
            sources = list(tx_playlist_sources)
            excluded = set(tx_playlist_excluded_files)
        refreshed_files, signature = build_playlist_snapshot(sources, excluded)
        previous = {path.casefold() for path in tx_playlist_files}
        current = {path.casefold() for path in refreshed_files}
        added = len(current - previous)
        removed = len(previous - current)
        tx_playlist_files[:] = refreshed_files
        with playlist_watch_lock:
            playlist_watch_revision += 1
            playlist_watch_signature = signature
        if playlist_ui_refresh_callback is not None:
            try:
                playlist_ui_refresh_callback()
            except tk.TclError:
                pass
        if save:
            schedule_tx_settings_save()
        return added, removed

    tx_setting_variables = (
        tx_station_name_var, tx_description_var, tx_frequency_var, tx_mode_var,
        tx_drm_service_label_var, tx_drm_service_id_var, tx_drm_language_var,
        tx_drm_programme_type_var, tx_drm_iso_language_var, tx_drm_iso_country_var,
        tx_drm_audio_codec_var, tx_drm_interleaving_var,
        tx_power_var, tx_audio_source_var, tx_audio_device_var,
        tx_playlist_shuffle_var, tx_playlist_loop_var, tx_cw_text_var,
        tx_cw_wpm_var, tx_cw_loop_var, tx_allow_overlap_var, tx_logo_url_var,
        tx_details_var, tx_page_layout_var, tx_fade_enabled_var,
        tx_fade_seconds_var, tx_fade_min_dbm_var, tx_fade_shape_var,
        tx_agc_enabled_var, tx_agc_target_var, tx_mic_gain_var, tx_bass_var,
        tx_treble_var, tx_presence_var, tx_drive_var, tx_ssb_bandwidth_var,
        tx_ssb_low_cut_var, tx_ssb_high_cut_var, tx_am_bandwidth_var,
        tx_am_pre_emphasis_var,
    )
    for setting_variable in tx_setting_variables:
        setting_variable.trace_add("write", schedule_tx_settings_save)

    def update_tx_power_readout(*_args) -> None:
        try:
            value = float(tx_power_var.get())
            tx_power_readout_var.set(f"{value:.0f} dBm")
        except (TypeError, ValueError):
            tx_power_readout_var.set("Invalid power")

    tx_power_var.trace_add("write", update_tx_power_readout)
    update_tx_power_readout()
    tx_transmit_button: tk.Button | None = None

    discord_authenticated = False
    auth_restore_started = False
    auth_enabled = False
    users_listing_received = False
    station_rows: list[dict[str, Any]] = []
    server_band_min_hz: int | None = None
    server_band_max_hz: int | None = None
    tx_negative_status_reason: str | None = None
    tx_negative_status_count = 0
    tx_ptt_confirmed = False
    tx_terminal_server_message: str | None = None
    station_search_var = tk.StringVar(root, "")
    station_cards_frame: tk.Frame | None = None
    selected_station_id = ""
    image_cache: dict[tuple[str, int], Any] = {}
    image_loading: set[str] = set()
    image_labels: dict[str, list[tuple[tk.Label, int, str]]] = {}
    current_page = "transmit"
    shell_frame: tk.Frame | None = None
    content_frame: tk.Frame | None = None
    nav_frame: tk.Frame | None = None
    active_scroll_canvas: tk.Canvas | None = None
    SCROLL_PIXELS_PER_NOTCH = 102

    def scroll_active_canvas(event, direction: int | None = None):
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
            if not canvas.bbox("all"):
                return
        except tk.TclError:
            return

        if direction is not None:
            scroll_delta = direction * SCROLL_PIXELS_PER_NOTCH
        else:
            delta = getattr(event, "delta", 0)
            if not delta:
                return
            scroll_delta = -(float(delta) / 120.0) * SCROLL_PIXELS_PER_NOTCH

        canvas.yview_scroll(int(round(scroll_delta)), "units")
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
            return

        image_loading.add(url)

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
            return label

        key = (url, size)
        photo = image_cache.get(key)
        if photo is not None:
            label.configure(image=photo, text="", width=size, height=size, bg=surface)
            label.image = photo
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
            return

        applied = 0
        for label, size, role in waiting:
            try:
                if not label.winfo_exists():
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
            except Exception as exc:
                pass

        if waiting and not applied:
            pass

    def bind_station_card(
        widget: tk.Widget,
        station_id: str,
        *,
        card_root: bool = True,
    ) -> None:
        widget.configure(cursor="hand2")
        widget.bind(
            "<Button-1>",
            lambda _event, target=station_id: open_station(target),
            add="+",
        )
        if card_root:
            widget.configure(
                takefocus=True,
                highlightthickness=2,
                highlightbackground=palette["outline"],
                highlightcolor=palette["primary"],
            )

            def open_with_keyboard(_event=None, target=station_id):
                open_station(target)
                return "break"

            widget.bind("<Return>", open_with_keyboard, add="+")
            widget.bind("<space>", open_with_keyboard, add="+")
            widget.bind(
                "<FocusIn>",
                lambda _event: widget.configure(highlightbackground=palette["primary"]),
                add="+",
            )
            widget.bind(
                "<FocusOut>",
                lambda _event: widget.configure(highlightbackground=palette["outline"]),
                add="+",
            )
            widget.bind(
                "<Enter>",
                lambda _event: widget.configure(highlightbackground=palette["primary"]),
                add="+",
            )
            widget.bind(
                "<Leave>",
                lambda _event: widget.configure(
                    highlightbackground=(
                        palette["primary"] if widget.focus_get() is widget else palette["outline"]
                    )
                ),
                add="+",
            )
        for child in widget.winfo_children():
            bind_station_card(child, station_id, card_root=False)

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
        current_page = "transmit"
        build_shell()

    def on_shutdown() -> None:
        playlist_watch_stop.set()
        if tray_icon is not None:
            try:
                tray_icon.stop()
            except Exception as exc:
                pass
        transmitter.stop(wait=1.0)
        client.disconnect()
        save_tx_settings_now()
        auth_manager.close()
        root.destroy()

    def restore_from_tray() -> None:
        def show_window() -> None:
            if tray_icon is not None:
                try:
                    tray_icon.visible = False
                except Exception as exc:
                    pass
            try:
                root.deiconify()
                root.state("normal")
                root.lift()
                root.focus_force()
            except tk.TclError:
                pass

        try:
            root.after(0, show_window)
        except tk.TclError:
            pass

    def quit_from_tray() -> None:
        try:
            root.after(0, on_shutdown)
        except tk.TclError:
            pass

    def initialize_tray_icon(icon: Any) -> None:
        icon.visible = False
        tray_setup_ready.set()

    def minimize_to_tray(attempt: int = 0) -> None:
        if os.name != "nt" or tray_icon is None:
            return
        try:
            if root.state() != "iconic":
                return
            if not tray_setup_ready.is_set():
                if attempt < 40:
                    root.after(50, lambda: minimize_to_tray(attempt + 1))
                return
            tray_icon.visible = True
            root.withdraw()
        except Exception as exc:
            try:
                root.deiconify()
            except tk.TclError:
                pass

    def handle_window_unmap(event: Any) -> None:
        if event.widget is root:
            try:
                root.after_idle(minimize_to_tray)
            except tk.TclError:
                pass

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
            status_message = station_status_var.get().strip()
            status = status_message.casefold()
            if "error" in status or "failed" in status:
                message = f"{status_message} Select Refresh to try again."
            elif (
                not users_listing_received
                or status.startswith(("waiting for", "loading", "refreshing"))
            ):
                message = status_message or "Waiting for TXITY station data."
            elif query:
                message = "No stations match that search."
            else:
                message = "TXITY returned an empty station list. Select Refresh to try again."
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
        if not station_rows:
            update_station_tree()
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
            "TXITPY",
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
            "Your Discord account is required to use the TXITPY desktop app. "
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
            yscrollincrement=1,
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

        make_label(
            page,
            "Search stations",
            font=("Segoe UI", 9, "bold"),
            color=palette["onSurfaceVariant"],
            background=palette["background"],
        ).pack(anchor="w", pady=(0, 5))

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
            highlightthickness=2,
            highlightbackground=palette["outline"],
            highlightcolor=palette["primary"],
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
        if transmitter.is_busy and data.get("ptt") is True:
            if not tx_ptt_confirmed:
                tx_ptt_confirmed = True
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
                        if tx_ptt_confirmed:
                            message = (
                                "TXITY reported that PTT stopped after confirming the transmission: "
                                f"{reason_text} (reason code {reason_key})."
                            )
                        else:
                            message = (
                                "TXITY did not confirm the PTT request: "
                                f"{reason_text} (reason code {reason_key})."
                            )
                        tx_terminal_server_message = message
                        tx_status_var.set(message)
                        transmitter.stop()
            else:
                tx_negative_status_reason = None
                tx_negative_status_count = 0

    def watch_playlist_sources() -> None:
        """Refresh imported folders and files when their contents change on disk."""
        nonlocal playlist_watch_signature
        while not playlist_watch_stop.wait(4.0):
            with playlist_watch_lock:
                sources = list(tx_playlist_sources)
                excluded = set(tx_playlist_excluded_files)
                revision = playlist_watch_revision
            if not sources:
                continue

            try:
                files, signature = build_playlist_snapshot(sources, excluded)
            except Exception as exc:
                details = safe_error_text(str(exc), 300)
                continue

            if playlist_watch_stop.is_set():
                break

            with playlist_watch_lock:
                if revision != playlist_watch_revision:
                    continue
                if signature == playlist_watch_signature:
                    continue
                playlist_watch_signature = signature

            client.events.put({
                "kind": "playlist_refresh",
                "files": files,
                "revision": revision,
            })

    def drain_events() -> None:
        nonlocal discord_authenticated, auth_restore_started, auth_enabled, station_rows, current_page, tx_profile_initialized
        nonlocal users_listing_received
        nonlocal tx_negative_status_reason, tx_negative_status_count
        nonlocal tx_ptt_confirmed, tx_terminal_server_message
        while True:
            try:
                event = client.events.get_nowait()
            except queue.Empty:
                break

            kind = event.get("kind")
            if kind == "connection":
                text = str(event.get("text", ""))
                connection_var.set(text)

            elif kind == "status":
                apply_status(event.get("data", {}))

            elif kind == "tx_state":
                state = str(event.get("state") or "")
                if state == "starting":
                    tx_negative_status_reason = None
                    tx_negative_status_count = 0
                    tx_ptt_confirmed = False
                    tx_terminal_server_message = None
                elif state == "stopped":
                    tx_negative_status_reason = None
                    tx_negative_status_count = 0
                    tx_ptt_confirmed = False
                if state == "stopped" and tx_terminal_server_message:
                    tx_status_var.set(tx_terminal_server_message)
                else:
                    tx_status_var.set(str(event.get("text") or state))
                if tx_audio_source_var.get() == "Audio playlist":
                    if state == "transmitting":
                        tx_playlist_status_var.set("Connected. Loading the first track...")
                    elif state == "stopped" and (
                        tx_playlist_status_var.get().startswith("Preparing")
                        or tx_playlist_status_var.get().startswith("Playing")
                        or tx_playlist_status_var.get().startswith("Connected")
                    ):
                        tx_playlist_status_var.set("Playlist stopped before completion.")
                update_transmit_button()

            elif kind == "tx_error":
                details = safe_error_text(str(event.get("text") or "Transmission failed."), 800)
                tx_status_var.set(f"Transmission error: {details}")
                update_transmit_button()

            elif kind == "tx_audio_warning":
                details = safe_error_text(str(event.get("text") or ""), 300)
                if details:
                    tx_status_var.set(f"Audio input warning: {details}")

            elif kind == "tx_server_message":
                data = event.get("data", {})
                if isinstance(data, dict):
                    message_type = str(data.get("type") or "")
                    server_text = safe_error_text(
                        str(data.get("message") or data.get("reason") or message_type),
                        500,
                    )
                    tx_status_var.set(f"TXITY: {server_text}")
                    if message_type in {"kicked", "banned"}:
                        transmitter.stop()

            elif kind == "playlist_refresh":
                revision = event.get("revision")
                with playlist_watch_lock:
                    current_revision = playlist_watch_revision
                if revision != current_revision:
                    continue

                refreshed_files = event.get("files")
                if not isinstance(refreshed_files, list):
                    continue
                previous_files = {path.casefold() for path in tx_playlist_files}
                tx_playlist_files[:] = [
                    str(path) for path in refreshed_files if isinstance(path, str)
                ]
                current_files = {path.casefold() for path in tx_playlist_files}
                added = len(current_files - previous_files)
                removed = len(previous_files - current_files)

                if playlist_ui_refresh_callback is not None:
                    try:
                        playlist_ui_refresh_callback()
                    except tk.TclError:
                        pass
                schedule_tx_settings_save()

                if transmitter.is_busy:
                    tx_playlist_status_var.set(
                        f"Playlist changed on disk ({added} added, {removed} removed). "
                        "Changes apply the next time you start playback."
                    )
                elif added or removed:
                    tx_playlist_status_var.set(
                        f"Playlist refreshed from disk: {added} added, {removed} removed."
                    )
                else:
                    tx_playlist_status_var.set("Playlist files changed on disk. List refreshed.")

            elif kind == "tx_playlist_track":
                track_path = str(event.get("path") or "")
                track_name = Path(track_path).name or "Audio track"
                tx_playlist_status_var.set(
                    f"Playing {event.get('index', 0)} of {event.get('count', 0)}: {track_name}"
                )

            elif kind == "tx_playlist_finished":
                tx_playlist_status_var.set("Playlist finished.")

            elif kind == "tx_playlist_error":
                details = safe_error_text(str(event.get("text") or "Unknown audio decoder error."), 500)
                tx_playlist_status_var.set(f"Playlist error: {details}")

            elif kind == "config":
                config = event.get("data", {})
                if isinstance(config, dict):
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
                users_listing_received = True
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
                        pass
                    else:
                        pass
                    profile_details = str(station.get("details_text") or "").strip()
                    if profile_details:
                        pass
                    else:
                        pass
                update_station_tree()

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

            elif kind == "stations_error":
                details = safe_error_text(
                    str(event.get("text", "Station refresh failed.")),
                    max_chars=1_200,
                )
                station_status_var.set(details)
                update_station_tree()

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
                    current_page = "transmit"
                    build_shell()
                    client.connect()
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
                auth_status_var.set(details)

            elif kind == "socket_error":
                details = safe_error_text(
                    str(event.get("text", "Unknown status connection error")),
                    max_chars=1_500,
                )
                connection_var.set("Live status unavailable")

            elif kind == "error":
                details = safe_error_text(
                    str(event.get("text", "Network error")),
                    max_chars=1_500,
                )
                if not auth_enabled:
                    auth_status_var.set(details)

        root.after(250, drain_events)

    def periodic_station_refresh() -> None:
        if discord_authenticated:
            client.refresh_stations()
        root.after(60_000, periodic_station_refresh)

    if os.name == "nt":
        try:
            from system_tray import create_tray_icon

            tray_icon = create_tray_icon(
                on_restore=restore_from_tray,
                on_quit=quit_from_tray,
            )
            tray_icon.run_detached(setup=initialize_tray_icon)
        except Exception as exc:
            tray_icon = None

    root.bind("<Unmap>", handle_window_unmap, add="+")

    build_shell()
    root.protocol("WM_DELETE_WINDOW", on_shutdown)
    root.after(250, drain_events)
    root.after(60_000, periodic_station_refresh)
    playlist_watch_thread = threading.Thread(
        target=watch_playlist_sources,
        daemon=True,
        name="txitpy-playlist-watch",
    )
    playlist_watch_thread.start()
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
