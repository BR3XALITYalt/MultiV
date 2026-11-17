"""TXITY station directory models, parsing, and media helpers."""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from discord_auth import safe_error_text
from transmitter import parse_rf_frequency

BASE_URL = "https://txity.radiodiscussion.org"
USER_AGENT = "MultiV/0.1 (+https://github.com/BR3XALITYalt/TXITPY)"
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
