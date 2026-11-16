"""TXITPY desktop companion for the public TXITY station directory and status feed.

This release is read-only. It does not send transmitter settings, PTT commands,
or audio frames, and it never reuses credentials captured from a browser session.
"""
from __future__ import annotations

import json
import queue
import threading
import uuid
import webbrowser
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

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
        self.ws: Any = None
        self.ws_thread: threading.Thread | None = None

    def connect(self) -> None:
        if self.ws_thread and self.ws_thread.is_alive():
            return
        self.stop_event.clear()
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

        # Generate a fresh anonymous session. Never reuse captured browser IDs,
        # session secrets, login tokens, or Discord credentials.
        query = urlencode({
            "session": str(uuid.uuid4()),
            "sessionSecret": str(uuid.uuid4()),
        })
        socket_url = f"{CONTROL_URL}?{query}"

        def on_open(ws: websocket.WebSocketApp) -> None:
            self.events.put({"kind": "connection", "text": "Connected (public status only)"})
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
                self.events.put({"kind": "socket_error", "text": str(error)})

        def on_close(_ws: websocket.WebSocketApp, _code: Any, _reason: Any) -> None:
            if not self.stop_event.is_set():
                self.events.put({"kind": "connection", "text": "Status connection closed"})

        try:
            self.ws = websocket.WebSocketApp(
                socket_url,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            while not self.stop_event.is_set():
                self.ws.run_forever(
                    origin=BASE_URL,
                    ping_interval=25,
                    ping_timeout=10,
                    reconnect=0,
                )
                if not self.stop_event.is_set():
                    self.events.put({"kind": "connection", "text": "Reconnecting to status feed..."})
                    self.stop_event.wait(4)
        except Exception as exc:
            if not self.stop_event.is_set():
                self.events.put({"kind": "socket_error", "text": str(exc)})
        finally:
            self.ws = None


def build_app() -> None:
    import bootstack as bs

    client = TxityClient()
    connection = bs.Signal("Disconnected")
    center = bs.Signal("Not reported")
    band = bs.Signal("Not reported")
    sample_rate = bs.Signal("Not reported")
    transmitters = bs.Signal("Not reported")
    clients = bs.Signal("Not reported")
    updated = bs.Signal("Waiting for server data")
    summary = bs.Signal("Connect to load public station and server information.")

    with bs.AppShell(
        title="TXITPY | TXITY Desktop",
        size=(1120, 760),
        theme="bootstrap-dark",
        on_close=client.disconnect,
    ) as shell:
        with shell.page_nav() as nav:
            with nav.add_page("overview", text="Overview", icon="house", padding=20, gap=12):
                bs.Label("TXITY", font="heading-lg")
                bs.Label("Public server monitor and station directory", accent="secondary")
                bs.Divider()
                bs.Label(textsignal=connection, font="heading-md", accent="primary")
                with bs.Row(gap=12):
                    bs.Button("Connect", accent="primary", on_click=client.connect)
                    bs.Button("Disconnect", variant="outline", on_click=client.disconnect)
                    bs.Button("Open TXITY", variant="outline", on_click=lambda: webbrowser.open(BASE_URL))
                bs.Label("Server status", font="heading-md")
                with bs.Grid(columns=2, gap=10):
                    with bs.Card(padding=12, gap=4):
                        bs.Label("Center frequency", accent="secondary")
                        bs.Label(textsignal=center, font="heading-lg")
                    with bs.Card(padding=12, gap=4):
                        bs.Label("Frequency range", accent="secondary")
                        bs.Label(textsignal=band, font="heading-lg")
                    with bs.Card(padding=12, gap=4):
                        bs.Label("Sample rate", accent="secondary")
                        bs.Label(textsignal=sample_rate, font="heading-lg")
                    with bs.Card(padding=12, gap=4):
                        bs.Label("Active transmitters", accent="secondary")
                        bs.Label(textsignal=transmitters, font="heading-lg")
                    with bs.Card(padding=12, gap=4):
                        bs.Label("Connected clients", accent="secondary")
                        bs.Label(textsignal=clients, font="heading-lg")
                bs.Label(textsignal=summary, wrap_width=760)
                bs.Label(textsignal=updated, accent="secondary", font="caption")

            with nav.add_page("stations", text="Stations", icon="list", padding=20, gap=10):
                bs.Label("Station directory", font="heading-lg")
                bs.Label(
                    "Public station information from TXITY. Sort columns or search the list.",
                    accent="secondary",
                )
                bs.Button("Refresh station list", accent="primary", on_click=client.refresh_stations)
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

            with nav.add_page("about", text="Connection", icon="info-circle", padding=20, gap=10):
                bs.Label("Connection and scope", font="heading-lg")
                bs.Label("Server", font="heading-md")
                bs.Label(BASE_URL, accent="primary")
                bs.Label(
                    "This app currently runs in public viewer mode. It reads the public station page, "
                    "public configuration, and status messages only.",
                    wrap_width=760,
                )
                bs.Label(
                    "Transmitter control, microphone/MP3 audio, profile editing, comments, and Discord "
                    "account actions are not implemented. TXITY requires its official Discord sign-in "
                    "for transmitter and station-edit permissions.",
                    wrap_width=760,
                )
                bs.Label(
                    "No captured browser session keys or login tokens are included. This client does "
                    "not send transmitter configuration, push-to-talk commands, or audio data.",
                    wrap_width=760,
                )
                bs.Button("Open TXITY login page", accent="primary", on_click=lambda: webbrowser.open(BASE_URL))
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
                    if isinstance(config, dict) and center() == "Not reported":
                        center.set(format_frequency(config.get("centerHz")))
                elif kind == "stations":
                    rows = event.get("rows", [])
                    station_table.set_rows(rows)
                    updated.set(f"Station list refreshed: {len(rows)} entries")
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
