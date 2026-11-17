"""Upload station profile images to TXITY."""
from __future__ import annotations

import base64
import json
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from discord_auth import is_auth_token, safe_error_text
from station_directory import BASE_URL, normalize_media_url

UPLOAD_URL = f"{BASE_URL}/api/station/upload"
UPLOAD_USER_AGENT = (
    "Mozilla/5.0 (Android 16; Mobile; rv:160.0) "
    "Gecko/160.0 Firefox/160.0"
)
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
UPLOAD_TIMEOUT = 30
IMAGE_MIME_TYPES = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
    "GIF": "image/gif",
}
URL_KEYS = (
    "url",
    "imageUrl",
    "image_url",
    "logoUrl",
    "logo_url",
    "uploadedUrl",
    "uploaded_url",
    "fileUrl",
    "file_url",
    "src",
    "href",
)
CONTAINER_KEYS = ("data", "image", "file", "result", "upload")


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def image_file_to_data_url(path: str | Path) -> str:
    """Validate a supported image and encode its original bytes as a data URL."""
    image_path = Path(path).expanduser()
    try:
        if image_path.stat().st_size > MAX_UPLOAD_BYTES:
            raise ValueError("Choose an image smaller than 5 MB.")
        image_bytes = image_path.read_bytes()
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError(f"Could not read the selected image: {exc}") from exc

    if not image_bytes:
        raise ValueError("The selected image is empty.")
    if len(image_bytes) > MAX_UPLOAD_BYTES:
        raise ValueError("Choose an image smaller than 5 MB.")

    try:
        from io import BytesIO
        from PIL import Image

        with Image.open(BytesIO(image_bytes)) as image:
            image_format = (image.format or "").upper()
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                raise ValueError("The selected image has too many pixels.")
            image.verify()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("Choose a valid PNG, JPEG, WebP, or GIF image.") from exc

    mime_type = IMAGE_MIME_TYPES.get(image_format)
    if not mime_type:
        raise ValueError("Choose a PNG, JPEG, WebP, or GIF image.")

    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _extract_image_url(payload: Any) -> str:
    """Find a server-returned image URL in common TXITY response shapes."""
    if isinstance(payload, dict):
        for key in URL_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                result = normalize_media_url(value)
                if result:
                    return result
        for key in CONTAINER_KEYS:
            if key in payload:
                result = _extract_image_url(payload[key])
                if result:
                    return result
    elif isinstance(payload, list):
        for item in payload[:8]:
            result = _extract_image_url(item)
            if result:
                return result
    elif isinstance(payload, str):
        value = payload.strip()
        if value.startswith(("https://", "/", "//")):
            return normalize_media_url(value)
    return ""


def upload_station_image(
    session_id: str,
    auth_token: str,
    data_url: str,
    *,
    kind: str = "logo",
) -> str:
    """Upload one image using the active TXITY session and return its hosted URL."""
    try:
        uuid.UUID(session_id)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("The TXITY session ID is invalid.") from exc
    if not is_auth_token(auth_token):
        raise RuntimeError("Sign in with Discord before uploading a station image.")
    if kind != "logo":
        raise ValueError("TXITPY supports station logo uploads.")
    if (
        not isinstance(data_url, str)
        or len(data_url) > (MAX_UPLOAD_BYTES * 4 // 3) + 256
        or not data_url.startswith("data:image/")
        or ";base64," not in data_url
    ):
        raise ValueError("The selected image data is invalid or too large.")

    request_body = json.dumps({
        "sessionId": session_id,
        "authToken": auth_token,
        "kind": kind,
        "dataUrl": data_url,
    }, separators=(",", ":")).encode("utf-8")
    request = Request(
        UPLOAD_URL,
        data=request_body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": UPLOAD_USER_AGENT,
            "Referer": f"{BASE_URL}/",
        },
        method="POST",
    )

    try:
        opener = build_opener(_NoRedirectHandler())
        with opener.open(request, timeout=UPLOAD_TIMEOUT) as response:
            response_body = response.read(1024 * 1024 + 1)
            if len(response_body) > 1024 * 1024:
                raise ValueError("TXITY returned an oversized image-upload response.")
    except HTTPError as exc:
        raise RuntimeError(f"TXITY rejected the image upload (HTTP {exc.code}).") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"Could not upload the station logo: {safe_error_text(str(exc), 240)}") from exc

    try:
        payload = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("TXITY returned an unreadable image-upload response.") from exc

    if isinstance(payload, dict) and (
        payload.get("ok") is False or payload.get("success") is False
    ):
        detail = payload.get("error") or payload.get("message") or "TXITY rejected the image."
        raise RuntimeError(safe_error_text(str(detail), 300))

    image_url = _extract_image_url(payload)
    if not image_url:
        raise RuntimeError("TXITY accepted the request but did not return a supported image URL.")
    return image_url
