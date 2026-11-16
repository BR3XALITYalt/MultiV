import unittest

from main import (
    describe_status_socket_error,
    format_duration,
    format_frequency,
    parse_stations,
    parse_users_listing,
    normalize_media_url,
    discord_avatar_url,
    _clean_station_comments,
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
                "logoUrl": "/uploads/stations/example/logo.png",
                "details": "Long station profile details.",
                "discordId": "1234567890",
                "discordAvatarUrl": "https://cdn.discordapp.com/avatars/1234567890/avatar.png",
                "imageUrls": ["/uploads/stations/example/gallery.png"],
                "likes": 7,
                "commentCount": 1,
                "ratingAverage": 1.375,
                "comments": [{
                    "discordUsername": "listener",
                    "text": "Clear signal.",
                    "createdAt": "2026-11-15T18:00:00Z",
                    "discordAvatarUrl": "https://cdn.discordapp.com/avatars/222/avatar.png",
                }],
                "pageLayout": {
                    "height": 520,
                    "blocks": [{
                        "type": "text",
                        "text": "Welcome to the station.",
                        "left": 10,
                        "top": 12,
                        "width": 80,
                        "height": 70,
                        "fontFamily": "Georgia",
                        "fontSize": 22,
                        "fontWeight": 700,
                        "textAlign": "center",
                    }],
                },
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

        record = stations[0].as_record()
        self.assertEqual(record["logo_url"], "https://txity.radiodiscussion.org/uploads/stations/example/logo.png")
        self.assertEqual(record["details_text"], "Long station profile details.")
        self.assertEqual(record["discord_id"], "1234567890")
        self.assertEqual(record["avatar_url"], "https://cdn.discordapp.com/avatars/1234567890/avatar.png")
        self.assertEqual(record["image_urls"], ["https://txity.radiodiscussion.org/uploads/stations/example/gallery.png"])
        self.assertEqual(record["likes"], 7)
        self.assertEqual(record["comment_count"], 1)
        self.assertEqual(record["comments"][0]["author"], "listener")
        self.assertEqual(record["comments"][0]["text"], "Clear signal.")
        self.assertEqual(record["page_layout"]["height"], 520)
        self.assertEqual(record["page_layout"]["blocks"][0]["text"], "Welcome to the station.")

    def test_protocol_relative_and_object_media_urls_are_normalized(self):
        self.assertEqual(
            normalize_media_url("//cdn.discordapp.com/avatars/123/avatar.png?size=128"),
            "https://cdn.discordapp.com/avatars/123/avatar.png?size=128",
        )
        self.assertEqual(
            normalize_media_url({"url": "/uploads/stations/example/logo.png"}),
            "https://txity.radiodiscussion.org/uploads/stations/example/logo.png",
        )
        self.assertEqual(normalize_media_url("//untrusted.example/image.png"), "")

    def test_discord_avatar_hash_is_resolved_from_user_id(self):
        self.assertEqual(
            discord_avatar_url("a_abcdef123456", "123456789012345678"),
            "https://cdn.discordapp.com/avatars/123456789012345678/a_abcdef123456.gif?size=128",
        )

    def test_comment_avatar_accepts_nested_author_and_avatar_aliases(self):
        comments = _clean_station_comments([{
            "message": "Good signal.",
            "author": {
                "id": "123456789012345678",
                "username": "listener",
                "avatar": "a_abcdef123456",
            },
            "createdAt": "2026-11-15T18:00:00Z",
        }])
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0]["author"], "listener")
        self.assertEqual(comments[0]["text"], "Good signal.")
        self.assertEqual(
            comments[0]["avatar_url"],
            "https://cdn.discordapp.com/avatars/123456789012345678/a_abcdef123456.gif?size=128",
        )

    def test_details_can_supply_missing_station_description(self):
        _metrics, stations = parse_users_listing({
            "stations": [{
                "sessionId": "station-details",
                "name": "Details Radio",
                "details": {"description": "A longer station description."},
            }],
        })
        record = stations[0].as_record()
        self.assertEqual(record["description"], "A longer station description.")
        self.assertEqual(record["description_source"], "details")
        self.assertEqual(record["details_text"], "A longer station description.")

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
