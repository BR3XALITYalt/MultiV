import unittest

from main import format_frequency, parse_stations


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


if __name__ == "__main__":
    unittest.main()
