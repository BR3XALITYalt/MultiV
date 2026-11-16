import unittest

from main import (
    describe_status_socket_error,
    format_duration,
    format_frequency,
    parse_stations,
    parse_users_listing,
)


class StationParserTests(unittest.TestCase):
    def test_extracts_public_station_card(self):
        html = """
        <button class="station-row" data-session-id="station-1">
          <div class="station-card-main">
            <strong>Example Radio</strong>
            <p>Music and number markers.</p>
            <p class="station-owner-line">Discord: example</p>
          </div>
          <div class="station-card-side">
            <span class="station-frequency">7.250 MHz</span>
            <span class="station-rating-badge">★ 4.8 (5)</span>
            <span class="station-card-meta">AM · 2h 15m on air</span>
          </div>
        </button>
        """
        stations = parse_stations(html)
        self.assertEqual(len(stations), 1)

        station = stations[0]
        self.assertEqual(station.station_id, "station-1")
        self.assertEqual(station.name, "Example Radio")
        self.assertEqual(station.description, "Music and number markers.")
        self.assertEqual(station.frequency, "7.250 MHz")
        self.assertEqual(station.rating, "★ 4.8 (5)")
        self.assertEqual(station.mode, "AM")

    def test_ignores_unrelated_buttons(self):
        self.assertEqual(
            parse_stations('<button class="nav-link">Transmit</button>'),
            [],
        )

    def test_frequency_formatting(self):
        self.assertEqual(format_frequency(7_250_000), "7.250 MHz")
        self.assertEqual(format_frequency(12_500), "12.5 kHz")
        self.assertEqual(format_frequency("invalid"), "Not reported")

    def test_parses_users_json_metrics_and_station_fields(self):
        metrics, stations = parse_users_listing({
            "updatedAtLocal": "04:17:49",
            "webClients": 8,
            "webAudioClients": 5,
            "sdrppClients": 1,
            "sdrppStreamingClients": 0,
            "sessions": 9,
            "transmitting": 6,
            "stations": [{
                "sessionId": "station-123",
                "name": "Example Radio",
                "description": "Long description",
                "shortDescription": "Short description",
                "discordUsername": "example",
                "frequencyHz": 7_440_900,
                "frequency": "7.4409 MHz",
                "mode": "LSB+C",
                "ratingAverage": 1.375,
                "ratingCount": 48,
                "onAirSeconds": 4121,
                "comments": [{"text": "Ignored comment"}],
                "pageLayout": "{...}",
            }],
        })

        self.assertEqual(metrics["updatedAtLocal"], "04:17:49")
        self.assertEqual(metrics["webClients"], 8)
        self.assertEqual(metrics["webAudioClients"], 5)
        self.assertEqual(metrics["sdrppClients"], 1)
        self.assertEqual(metrics["sdrppStreamingClients"], 0)
        self.assertEqual(metrics["sessions"], 9)
        self.assertEqual(metrics["transmitting"], 6)
        self.assertEqual(len(stations), 1)

        row = stations[0].as_row()
        self.assertEqual(row["id"], "station-123")
        self.assertEqual(row["station"], "Example Radio")
        self.assertEqual(row["operator"], "example")
        self.assertEqual(row["frequency"], "7.4409 MHz")
        self.assertEqual(row["mode"], "LSB+C")
        self.assertEqual(row["on_air"], "1h 8m")
        self.assertEqual(row["rating"], "★ 1.38 (48)")
        self.assertEqual(row["description"], "Short description")
        self.assertNotIn("comments", row)
        self.assertNotIn("pageLayout", row)

    def test_users_json_uses_frequency_hz_when_formatted_frequency_is_missing(self):
        _metrics, stations = parse_users_listing({
            "stations": [{
                "sessionId": "station-456",
                "name": "Fallback Frequency",
                "frequencyHz": 7_250_000,
            }],
        })
        self.assertEqual(stations[0].frequency, "7.250 MHz")

    def test_users_json_rejects_invalid_top_level_data(self):
        with self.assertRaises(ValueError):
            parse_users_listing([])
        with self.assertRaises(ValueError):
            parse_users_listing({"stations": "not-an-array"})

    def test_formats_on_air_duration(self):
        self.assertEqual(format_duration(4121), "1h 8m")
        self.assertEqual(format_duration(125), "2m 5s")
        self.assertEqual(format_duration(8), "8s")
        self.assertEqual(format_duration(-1), "Not reported")

    def test_websocket_error_hides_credentials_in_socket_url(self):
        class HandshakeError(Exception):
            status_code = 403
            status_message = "Forbidden"

            def __str__(self):
                return (
                    "Handshake rejected at "
                    "wss://example.test/ws?authToken=secret-token"
                    "&sessionSecret=secret-session"
                )

        message = describe_status_socket_error(HandshakeError())
        self.assertIn("HTTP 403 Forbidden", message)
        self.assertIn("[WebSocket URL redacted]", message)
        self.assertNotIn("secret-token", message)
        self.assertNotIn("secret-session", message)


if __name__ == "__main__":
    unittest.main()
