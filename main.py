"""TXITPY desktop companion for the public TXITY station directory and status feed.

This release is read-only. It does not send transmitter settings, PTT commands,
or audio frames. Discord sign-in uses TXITY's official OAuth route and a one-use loopback callback.
"""
from __future__ import annotations

import json
import queue
import secrets
import threading
import uuid
import webbrowser
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from discord_auth import DiscordAuthManager, clean_discord_user, is_auth_token

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

    def as_row(self) -> dict[str, str]:
        return {
            "id": self.station_id,
            "station": self.name,
            "frequency": self.frequency,
            "mode": self.mode,
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
                html = fetch_text(f"{BASE_URL}/")
                stations = parse_stations(html)
                self.events.put({
                    "kind": "stations",
                    "rows": [station.as_row() for station in stations],
                })
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
                self.events.put({"kind": "error", "text": f"Station refresh failed: {exc}"})
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
                self.events.put({"kind": "status", "data": data})

        def on_error(_ws: websocket.WebSocketApp, error: Any) -> None:
            if not self.stop_event.is_set():
                # Never include a WebSocket URL or its authToken in error text.
                self.events.put({"kind": "socket_error", "text": "The TXITY status connection failed."})

        def on_close(_ws: websocket.WebSocketApp, _code: Any, _reason: Any) -> None:
            if not self.stop_event.is_set():
                self.events.put({"kind": "connection", "text": "Status connection closed"})

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
        except Exception:
            if not self.stop_event.is_set():
                self.events.put({"kind": "socket_error", "text": "The TXITY status connection failed."})
        finally:
            self.ws = None


def build_app() -> None:
    import bootstack as bs
    from bootstack.style import Theme, get_font_families, set_font_family

    Theme.from_existing(
        "bootstrap",
        name="txity",
        primary="#ffffff",
        neutral="#cfcfcf",
        dark={"background": "#000000", "foreground": "#ffffff"},
        surfaces={
            "dark": {
                "content": "#000000",
                "card": "#040404",
                "chrome": "#000000",
                "raised": "#0b0b0b",
                "overlay": "#111111",
                "input": "#000000",
            }
        },
    ).install(activate="txity-dark")

    # Use Lexend when the font is installed; otherwise retain Bootstack's
    # native system sans-serif fallback, as in the website's CSS stack.
    if "Lexend" in get_font_families():
        set_font_family("Lexend")

    client = TxityClient()
    auth_manager = DiscordAuthManager(client.events)
    auth_account = bs.Signal("Not signed in to Discord")
    auth_browser_status = bs.Signal("Waiting for TXITY server configuration.")
    connection = bs.Signal("Disconnected")
    center = bs.Signal("Not reported")
    band = bs.Signal("Not reported")
    sample_rate = bs.Signal("Not reported")
    transmitters = bs.Signal("Not reported")
    clients = bs.Signal("Not reported")
    updated = bs.Signal("Waiting for server data")
    summary = bs.Signal("Connect to load public station and server information.")

    def disconnect_discord() -> None:
        client.set_discord_auth(None, None)
        auth_account.set("Not connected to Discord")
        auth_browser_status.set("TXITPY disconnected its Discord session. The system browser may still be signed in.")

    def shutdown() -> None:
        client.disconnect()
        auth_manager.close()

    with bs.AppShell(
        title="TXITPY | TXITY Desktop",
        size=(1120, 760),
        min_size=(860, 620),
        theme="txity-dark",
        show_sidebar=False,
        sidebar_mode="hidden",
        nav_accent="primary",
        remember_window_state=True,
        on_close=shutdown,
    ) as shell:
        toolbar = shell.add_toolbar(
            surface="chrome",
            padding=10,
            show_border=True,
            button_variant="outline",
        )
        toolbar.add_label("TXITY", font="heading-md")
        toolbar.add_divider()
        toolbar.add_button(
            "Overview",
            icon="house",
            accent="primary",
            on_click=lambda: shell.navigate("overview"),
        )
        toolbar.add_button(
            "Stations",
            icon="list",
            accent="primary",
            on_click=lambda: shell.navigate("stations"),
        )
        toolbar.add_button(
            "Discord",
            icon="chat-dots",
            accent="primary",
            on_click=lambda: shell.navigate("about"),
        )
        toolbar.add_spacer()
        toolbar.add_widget(
            bs.Badge,
            textsignal=connection,
            accent="primary",
            variant="pill",
            font="caption",
        )

        with shell.page_nav() as nav:
            with nav.add_page("overview", text="Overview", icon="house", padding=24, gap=16):
                bs.Label("TXITY", font="display-xl")
                bs.Label(
                    "Public server monitor and station directory",
                    font="body-lg",
                    accent="secondary",
                )
                bs.Divider()
                with bs.Row(gap=12):
                    bs.Button("Connect", accent="primary", on_click=client.connect)
                    bs.Button("Disconnect", variant="outline", on_click=client.disconnect)
                    bs.Button("Open TXITY", variant="outline", on_click=lambda: webbrowser.open(BASE_URL))
                bs.Label("SERVER STATUS", font="label", accent="secondary")
                with bs.Grid(columns=3, gap=16):
                    with bs.Card(padding=18, gap=8, accent="primary"):
                        bs.Label("Center frequency", font="label", accent="secondary")
                        bs.Label(textsignal=center, font="display-lg")
                    with bs.Card(padding=18, gap=8, accent="primary"):
                        bs.Label("Frequency range", font="label", accent="secondary")
                        bs.Label(textsignal=band, font="display-lg")
                    with bs.Card(padding=18, gap=8, accent="primary"):
                        bs.Label("Sample rate", font="label", accent="secondary")
                        bs.Label(textsignal=sample_rate, font="display-lg")
                    with bs.Card(padding=18, gap=8, accent="primary"):
                        bs.Label("Active transmitters", font="label", accent="secondary")
                        bs.Label(textsignal=transmitters, font="display-lg")
                    with bs.Card(padding=18, gap=8, accent="primary"):
                        bs.Label("Connected clients", font="label", accent="secondary")
                        bs.Label(textsignal=clients, font="display-lg")
                with bs.Card(padding=18, gap=10, accent="primary"):
                    bs.Label(textsignal=summary, font="body-lg", wrap_width=760)
                    bs.Label(textsignal=updated, accent="secondary", font="caption")

            with nav.add_page("stations", text="Stations", icon="list", padding=24, gap=16):
                bs.Label("Station directory", font="display-lg")
                bs.Label(
                    "Public station information from TXITY. Sort columns or search the list.",
                    font="body-lg",
                    accent="secondary",
                )
                with bs.Card(padding=18, gap=14, accent="primary"):
                    bs.Button(
                        "Refresh station list",
                        accent="primary",
                        on_click=client.refresh_stations,
                    )
                    station_table = bs.DataTable(
                        columns=[
                            {"key": "station", "text": "Station", "width": 190},
                            {"key": "frequency", "text": "Frequency", "width": 115},
                            {"key": "mode", "text": "Mode", "width": 90},
                            {"key": "rating", "text": "Rating", "width": 110},
                            {"key": "description", "text": "Description"},
                        ],
                        rows=[],
                        searchable=True,
                        allow_filter=True,
                        paging_mode="virtual",
                        page_size=30,
                        show_status_bar=True,
                        id_field="id",
                        context_menus="headers",
                    )
                bs.Label(
                    "Station data is public and may be delayed by the server or network.",
                    accent="secondary",
                    font="caption",
                )

            with nav.add_page("about", text="Discord", icon="info-circle", padding=24, gap=16):
                bs.Label("Discord account", font="display-lg")
                with bs.Card(padding=22, gap=14, accent="primary"):
                    bs.Label(
                        textsignal=auth_account,
                        font="heading-md",
                        accent="primary",
                    )
                    bs.Label(
                        textsignal=auth_browser_status,
                        accent="secondary",
                    )
                    with bs.Row(gap=10):
                        bs.Button(
                            "Sign in with Discord",
                            accent="primary",
                            on_click=auth_manager.start_login,
                        )
                        bs.Button(
                            "Disconnect Discord",
                            variant="outline",
                            on_click=disconnect_discord,
                        )
                    bs.Divider()
                    bs.Label("Connection", font="heading-md")
                    bs.Label(BASE_URL, accent="primary")
                    bs.Label(
                        "TXITPY opens TXITY's official Discord sign-in route in your system browser. "
                        "A one-use loopback callback returns the signed token to this app.",
                        wrap_width=760,
                    )
                    bs.Label(
                        "The system browser keeps TXITY's normal website session. TXITPY keeps the "
                        "returned token in memory and supplies it to the status WebSocket; it does not "
                        "write the token to a settings file or log it.",
                        wrap_width=760,
                    )
                    bs.Label(
                        "The desktop client remains read-only for radio operation. It sends status requests and TXITY's "
                        "logout notification, never transmitter configuration, push-to-talk commands, or audio frames.",
                        wrap_width=760,
                    )
                    bs.Button(
                        "Open TXITY website",
                        variant="outline",
                        on_click=lambda: webbrowser.open(BASE_URL),
                    )
                    bs.Button(
                        "Open project repository",
                        variant="outline",
                        on_click=lambda: webbrowser.open("https://github.com/BR3XALITYalt/TXITPY"),
                    )

        def apply_status(data: dict[str, Any]) -> None:
            center.set(format_frequency(data.get("centerHz")))
            low, high = data.get("bandMinHz"), data.get("bandMaxHz")
            if isinstance(low, (int, float)) and isinstance(high, (int, float)):
                band.set(f"{format_frequency(low)} - {format_frequency(high)}")
            rate = data.get("sampleRate")
            sample_rate.set(
                f"{rate / 1_000_000:.3f} MS/s"
                if isinstance(rate, (int, float)) else "Not reported"
            )
            transmitters.set(str(data.get("transmittingCount", "Not reported")))
            web_clients, sdrpp_clients = data.get("webClients"), data.get("sdrppClients")
            if isinstance(web_clients, int) and isinstance(sdrpp_clients, int):
                clients.set(f"{web_clients} web / {sdrpp_clients} SDR++")
            protection = "enabled" if data.get("protection") else "not reported"
            summary.set(
                f"Server protection: {protection}. "
                f"TXITY reports {data.get('transmittingCount', 'an unknown number')} active transmitters."
            )

        def drain_events() -> None:
            while True:
                try:
                    event = client.events.get_nowait()
                except queue.Empty:
                    break
                kind = event.get("kind")
                if kind == "connection":
                    connection.set(str(event.get("text", "")))
                elif kind == "status":
                    apply_status(event.get("data", {}))
                    updated.set("Live status received")
                elif kind == "config":
                    config = event.get("data", {})
                    if isinstance(config, dict):
                        if center() == "Not reported":
                            center.set(format_frequency(config.get("centerHz")))
                        auth_manager.configure(
                            config.get("discordAuthUrl"),
                            config.get("secureDiscordAuth") is True,
                        )
                        if config.get("secureDiscordAuth") is True:
                            auth_browser_status.set("TXITY secure Discord sign-in is available.")
                        else:
                            auth_browser_status.set("TXITY secure Discord sign-in is not enabled.")
                elif kind == "stations":
                    rows = event.get("rows", [])
                    station_table.set_rows(rows)
                    updated.set(f"Station list refreshed: {len(rows)} entries")
                elif kind == "auth_token":
                    token = event.get("token")
                    user = clean_discord_user(event.get("user"))
                    if is_auth_token(token):
                        client.set_discord_auth(token, user)
                        display_name = (user or {}).get("globalName") or (user or {}).get("username") or "Discord account"
                        auth_account.set(f"Signed in as {display_name}")
                        auth_browser_status.set("TXITY returned a signed session through the local callback.")
                elif kind == "auth_signed_out":
                    client.set_discord_auth(None, None)
                    auth_account.set("Not signed in to Discord")
                    auth_browser_status.set("TXITY's Discord session was disconnected.")
                elif kind == "auth_browser":
                    auth_browser_status.set(str(event.get("text", "")))
                elif kind == "auth_error":
                    auth_browser_status.set(str(event.get("text", "Sign-in browser error")))
                elif kind in {"error", "socket_error"}:
                    summary.set(str(event.get("text", "Network error")))
                    if kind == "socket_error":
                        connection.set("Live status unavailable; station directory can still be refreshed")

        shell.schedule.every(250, drain_events)
        shell.schedule.every(60_000, client.refresh_stations)
        client.connect()

    shell.run()


if __name__ == "__main__":
    build_app()
