"""TXITPY desktop companion for the public TXITY station directory and status feed.

This release is read-only. It does not send transmitter settings, PTT commands,
or audio frames. Discord sign-in uses TXITY's official OAuth route and a one-use loopback callback.
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
from urllib.parse import urlencode
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

BASE_URL = "https://txity.radiodiscussion.org"
CONTROL_URL = "wss://txity.radiodiscussion.org/ws/control"
USER_AGENT = "TXITPY/0.1 (+https://github.com/BR3XALITYalt/TXITPY)"
HTTP_TIMEOUT = 12


@dataclass(slots=True)
class Station:
    station_id: str
    name: str = "Unknown station"
    description: str = ""
    frequency: str = ""
    rating: str = ""
    mode: str = ""
    details: str = ""
    operator: str = ""
    on_air: str = ""

    def as_row(self) -> dict[str, str]:
        return {
            "id": self.station_id,
            "station": self.name,
            "operator": self.operator,
            "frequency": self.frequency,
            "mode": self.mode,
            "on_air": self.on_air,
            "rating": self.rating,
            "description": self.description,
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


def parse_users_listing(payload: Any) -> tuple[dict[str, Any], list[Station]]:
    """Parse the public TXITY users.json response without retaining comment data."""
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

        description = item.get("shortDescription")
        if not isinstance(description, str) or not description.strip():
            description = item.get("description")
        if not isinstance(description, str):
            description = ""

        frequency = item.get("frequency")
        if not isinstance(frequency, str) or not frequency.strip():
            frequency = format_frequency(item.get("frequencyHz"))

        mode = item.get("mode")
        if not isinstance(mode, str):
            mode = ""

        average = item.get("ratingAverage")
        count = item.get("ratingCount")
        if (
            isinstance(average, (int, float))
            and not isinstance(average, bool)
            and isinstance(count, int)
            and not isinstance(count, bool)
            and count > 0
        ):
            rating = f"★ {average:.2f} ({count})"
        else:
            rating = "Not rated"

        operator = item.get("discordUsername")
        if not isinstance(operator, str):
            operator = ""

        stations.append(Station(
            station_id=station_id.strip(),
            name=name.strip(),
            description=description.strip(),
            frequency=frequency.strip(),
            rating=rating,
            mode=mode.strip(),
            operator=operator.strip(),
            on_air=format_duration(item.get("onAirSeconds")),
        ))

    return metrics, stations


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

    def connect(self) -> None:
        if self.ws_thread and self.ws_thread.is_alive():
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
                    "rows": [station.as_row() for station in stations],
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
            ws.send(json.dumps({"type": "status"}))

            def heartbeat() -> None:
                while not self.stop_event.wait(1.0):
                    try:
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
            if isinstance(data, dict) and data.get("type") == "status":
                if not status_received.is_set():
                    status_received.set()
                    self.events.put({
                        "kind": "connection",
                        "text": "Connected; receiving live TXITY status.",
                    })
                self.events.put({"kind": "status", "data": data})

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
    from material_theme import MATERIAL_3_EXPRESSIVE, apply_tk_theme

    client = TxityClient()
    auth_manager = DiscordAuthManager(client.events)

    theme_mode = _system_theme_mode()
    palette = apply_tk_theme(None, theme_mode)

    root = tk.Tk()
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
    station_rows: list[dict[str, str]] = []
    station_tree: ttk.Treeview | None = None
    station_search_var = tk.StringVar(root, "")
    current_page = "overview"
    shell_frame: tk.Frame | None = None
    content_frame: tk.Frame | None = None
    nav_frame: tk.Frame | None = None
    auth_status_label: tk.Label | None = None

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
    ) -> tk.Button:
        if kind == "primary":
            background = palette["primary"]
            foreground = palette["onPrimary"]
            active_background = palette["primaryContainer"]
            active_foreground = palette["onPrimaryContainer"]
        elif kind == "tonal":
            background = palette["secondaryContainer"]
            foreground = palette["onSecondaryContainer"]
            active_background = palette["surfaceContainerHighest"]
            active_foreground = palette["onSurface"]
        else:
            background = palette["surfaceContainerLow"]
            foreground = palette["onSurface"]
            active_background = palette["surfaceContainerHigh"]
            active_foreground = palette["onSurface"]
        return tk.Button(
            parent,
            text=text,
            command=command,
            font=("Segoe UI", 10, "bold" if kind == "primary" else "normal"),
            fg=foreground,
            bg=background,
            activeforeground=active_foreground,
            activebackground=active_background,
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            padx=padx,
            pady=pady,
            cursor="hand2",
            takefocus=True,
        )

    def make_card(
        parent: tk.Misc,
        *,
        background: str | None = None,
        padding: int = 18,
    ) -> tk.Frame:
        return tk.Frame(
            parent,
            bg=background or palette["surfaceContainerLow"],
            padx=padding,
            pady=padding,
            borderwidth=1,
            relief="solid",
            highlightthickness=0,
            highlightbackground=palette["outlineVariant"],
        )

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
        nonlocal discord_authenticated
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
        client.disconnect()
        auth_manager.close()
        root.destroy()

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

    def update_station_tree() -> None:
        if station_tree is None:
            return
        try:
            if not station_tree.winfo_exists():
                return
        except tk.TclError:
            return

        query = station_search_var.get().strip().casefold()
        for child in station_tree.get_children():
            station_tree.delete(child)

        shown = 0
        for row in station_rows:
            searchable = " ".join(str(value) for value in row.values()).casefold()
            if query and query not in searchable:
                continue
            station_tree.insert(
                "",
                "end",
                values=(
                    row.get("station", ""),
                    row.get("operator", ""),
                    row.get("frequency", ""),
                    row.get("mode", ""),
                    row.get("on_air", ""),
                    row.get("rating", ""),
                    row.get("description", ""),
                ),
            )
            shown += 1

        station_count_var.set(
            f"{shown} of {len(station_rows)} stations"
            if query else f"{len(station_rows)} stations"
        )

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
            padding=32,
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

    def show_overview(parent: tk.Frame) -> None:
        canvas = tk.Canvas(
            parent,
            background=palette["background"],
            highlightthickness=0,
            borderwidth=0,
        )
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        page = tk.Frame(canvas, background=palette["background"])
        page_id = canvas.create_window((0, 0), window=page, anchor="nw")

        def update_scroll_region(_event: Any = None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfigure(page_id, width=canvas.winfo_width())

        page.bind("<Configure>", update_scroll_region)
        canvas.bind("<Configure>", update_scroll_region)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

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
        make_button(actions, "Disconnect", client.disconnect, kind="tonal").pack(side="left", padx=(0, 8))
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
                padx=(0 if column == 0 else 6, 0 if column == 3 else 6),
                pady=(0, 12),
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

        info = make_card(page, padding=18)
        info.pack(fill="x", pady=(4, 16))
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
        nonlocal station_tree

        page = tk.Frame(parent, bg=palette["background"])
        page.pack(fill="both", expand=True)

        make_label(
            page,
            "Station directory",
            font=("Segoe UI", 25, "bold"),
            background=palette["background"],
        ).pack(anchor="w", pady=(8, 2))
        make_label(
            page,
            "Live public station information from TXITY.",
            font=("Segoe UI", 10, "normal"),
            color=palette["onSurfaceVariant"],
            background=palette["background"],
        ).pack(anchor="w", pady=(0, 16))

        controls = tk.Frame(page, bg=palette["background"])
        controls.pack(fill="x", pady=(0, 10))

        search = ttk.Entry(
            controls,
            textvariable=station_search_var,
            style="Material.TEntry",
        )
        search.pack(side="left", fill="x", expand=True, ipady=7)
        search.bind("<KeyRelease>", lambda _event: update_station_tree())

        make_button(
            controls,
            "Refresh",
            refresh_station_list,
            kind="primary",
        ).pack(side="left", padx=(10, 0))

        status_bar = tk.Frame(page, bg=palette["background"])
        status_bar.pack(fill="x", pady=(0, 9))
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

        table_card = make_card(page, padding=10)
        table_card.pack(fill="both", expand=True)

        table_frame = tk.Frame(table_card, bg=palette["surfaceContainerLow"])
        table_frame.pack(fill="both", expand=True)

        columns = (
            "station",
            "operator",
            "frequency",
            "mode",
            "on_air",
            "rating",
            "description",
        )
        station_tree = ttk.Treeview(
            table_frame,
            columns=columns,
            show="headings",
            selectmode="browse",
            style="Material.Treeview",
        )
        headings = {
            "station": ("Station", 160, "w"),
            "operator": ("Operator", 135, "w"),
            "frequency": ("Frequency", 110, "w"),
            "mode": ("Mode", 95, "center"),
            "on_air": ("On air", 85, "center"),
            "rating": ("Rating", 115, "center"),
            "description": ("Description", 340, "w"),
        }
        for key, (title, width, anchor) in headings.items():
            station_tree.heading(key, text=title)
            station_tree.column(key, width=width, minwidth=70, anchor=anchor, stretch=key == "description")

        vertical = ttk.Scrollbar(
            table_frame,
            orient="vertical",
            command=station_tree.yview,
        )
        horizontal = ttk.Scrollbar(
            table_frame,
            orient="horizontal",
            command=station_tree.xview,
        )
        station_tree.configure(
            yscrollcommand=vertical.set,
            xscrollcommand=horizontal.set,
        )
        station_tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        table_frame.grid_rowconfigure(0, weight=1)
        table_frame.grid_columnconfigure(0, weight=1)
        update_station_tree()

    def show_discord(parent: tk.Frame) -> None:
        page = tk.Frame(parent, bg=palette["background"])
        page.pack(fill="both", expand=True)

        make_label(
            page,
            "Discord account",
            font=("Segoe UI", 25, "bold"),
            background=palette["background"],
        ).pack(anchor="w", pady=(8, 18))

        card = make_card(page, padding=24)
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
        nonlocal shell_frame, content_frame, nav_frame, station_tree

        for child in root.winfo_children():
            child.destroy()

        shell_frame = tk.Frame(root, bg=palette["background"])
        shell_frame.pack(fill="both", expand=True)

        topbar = tk.Frame(
            shell_frame,
            bg=palette["surface"],
            height=66,
            padx=20,
            pady=10,
        )
        topbar.pack(fill="x")
        topbar.pack_propagate(False)

        make_label(
            topbar,
            "TXITY",
            font=("Segoe UI", 17, "bold"),
            color=palette["primary"],
            background=palette["surface"],
        ).pack(side="left")
        make_label(
            topbar,
            "Desktop",
            font=("Segoe UI", 10, "normal"),
            color=palette["onSurfaceVariant"],
            background=palette["surface"],
        ).pack(side="left", padx=(10, 0))

        make_button(
            topbar,
            "Light mode" if theme_mode == "dark" else "Dark mode",
            toggle_theme,
            kind="tonal",
            padx=12,
            pady=7,
        ).pack(side="right")

        if discord_authenticated:
            make_label(
                topbar,
                variable=connection_var,
                font=("Segoe UI", 9, "normal"),
                color=palette["onSurfaceVariant"],
                background=palette["surface"],
            ).pack(side="right", padx=(0, 16))

            body = tk.Frame(shell_frame, bg=palette["background"])
            body.pack(fill="both", expand=True)

            nav_frame = tk.Frame(
                body,
                bg=palette["surfaceContainerLow"],
                width=190,
                padx=12,
                pady=18,
            )
            nav_frame.pack(side="left", fill="y")
            nav_frame.pack_propagate(False)

            make_label(
                nav_frame,
                "WORKSPACE",
                font=("Segoe UI", 8, "bold"),
                color=palette["onSurfaceVariant"],
                background=palette["surfaceContainerLow"],
            ).pack(anchor="w", padx=8, pady=(4, 12))

            navigation = (
                ("Overview", "overview"),
                ("Stations", "stations"),
                ("Discord", "discord"),
            )
            for label, destination in navigation:
                selected = current_page == destination
                button = tk.Button(
                    nav_frame,
                    text=label,
                    command=lambda target=destination: navigate(target),
                    anchor="w",
                    font=("Segoe UI", 10, "bold" if selected else "normal"),
                    fg=(
                        palette["onPrimaryContainer"]
                        if selected else palette["onSurfaceVariant"]
                    ),
                    bg=(
                        palette["primaryContainer"]
                        if selected else palette["surfaceContainerLow"]
                    ),
                    activeforeground=palette["onPrimaryContainer"],
                    activebackground=palette["primaryContainer"],
                    relief="flat",
                    borderwidth=0,
                    highlightthickness=0,
                    padx=14,
                    pady=11,
                    cursor="hand2",
                )
                button.pack(fill="x", pady=2)

            content_frame = tk.Frame(
                body,
                bg=palette["background"],
                padx=24,
                pady=20,
            )
            content_frame.pack(side="left", fill="both", expand=True)

            if current_page == "stations":
                show_stations(content_frame)
            elif current_page == "discord":
                show_discord(content_frame)
            else:
                show_overview(content_frame)
        else:
            nav_frame = None
            content_frame = tk.Frame(
                shell_frame,
                bg=palette["background"],
                padx=24,
                pady=20,
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
        center_var.set(format_frequency(data.get("centerHz")))
        low, high = data.get("bandMinHz"), data.get("bandMaxHz")
        if isinstance(low, (int, float)) and isinstance(high, (int, float)):
            band_var.set(f"{format_frequency(low)} - {format_frequency(high)}")
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

    def drain_events() -> None:
        nonlocal discord_authenticated, auth_restore_started, auth_enabled, station_rows
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
                apply_users_metrics(event.get("metrics", {}))
                station_status_var.set(
                    f"Loaded {len(station_rows)} stations from TXITY users.json."
                    if station_rows else
                    "TXITY users.json loaded successfully, but its stations array is empty."
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

            elif kind == "stations_error":
                details = safe_error_text(
                    str(event.get("text", "Station refresh failed.")),
                    max_chars=1_200,
                )
                station_status_var.set(details)
                summary_var.set(details)
                print(f"[TXITPY] {details}", file=sys.stderr, flush=True)

            elif kind == "auth_token":
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
