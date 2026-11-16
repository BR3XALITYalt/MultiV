import unittest
from urllib.parse import parse_qs, urlparse

from transmitter import (
    AUDIO_URL,
    FRAME_BYTES,
    SAMPLE_RATE_HZ,
    BATCH_SAMPLES,
    build_audio_socket_url,
    build_transmit_config,
    parse_rf_frequency,
    StreamingLinearResampler,
)


class TransmitterProtocolTests(unittest.TestCase):
    def test_audio_batch_matches_browser_worklet(self):
        self.assertEqual(BATCH_SAMPLES, 512)
        self.assertEqual(SAMPLE_RATE_HZ, 48_000)
        self.assertEqual(FRAME_BYTES, 2_048)

    def test_parse_frequency_units(self):
        self.assertEqual(parse_rf_frequency("7.250 MHz"), 7_250_000)
        self.assertEqual(parse_rf_frequency("7250 kHz"), 7_250_000)
        self.assertEqual(parse_rf_frequency("7250000 Hz"), 7_250_000)
        self.assertEqual(parse_rf_frequency("7.25"), 7_250_000)

    def test_rejects_invalid_frequency(self):
        for value in ("", "abc", "0", "-7 MHz", "7 GHz"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_rf_frequency(value)

    def test_builds_config_with_txity_current_config_fields(self):
        config = build_transmit_config(
            station_name="Example Radio",
            description="Test stream",
            frequency="7.250 MHz",
            mode="USB",
            station_dbm=50,
            allow_overlap=True,
            auth_token="test-token",
            cw_loop=False,
        )
        expected = {
            "type", "rfHz", "stationName", "stationDescription",
            "stationShortDescription", "stationLogoUrl", "stationDetails",
            "stationPageLayout", "discordAuthToken", "mode", "sampleRate",
            "stationDbm", "fadeMs", "fadeMinDbm", "fadeShape",
            "allowOverlapVisitors", "cwText", "cwWpm", "cwLoop",
            "agcEnabled", "agcTarget", "micGainDb", "bassDb", "trebleDb",
            "presenceDb", "drive", "ssbBandwidthHz", "ssbLowCutHz",
            "ssbHighCutHz", "amBandwidthHz", "amPreEmphasis",
        }
        self.assertEqual(set(config), expected)
        self.assertEqual(config["rfHz"], 7_250_000)
        self.assertEqual(config["sampleRate"], 48_000)
        self.assertEqual(config["mode"], "USB")
        self.assertTrue(config["allowOverlapVisitors"])
        self.assertFalse(config["cwLoop"])
        self.assertEqual(config["discordAuthToken"], "test-token")

    def test_validates_required_config_values(self):
        with self.assertRaisesRegex(ValueError, "Station name"):
            build_transmit_config(
                station_name="", description="", frequency="7.250 MHz",
                mode="AM", station_dbm=50, allow_overlap=False,
                auth_token="test-token",
            )
        with self.assertRaisesRegex(ValueError, "Discord"):
            build_transmit_config(
                station_name="Example", description="", frequency="7.250 MHz",
                mode="AM", station_dbm=50, allow_overlap=False,
                auth_token="",
            )
        with self.assertRaisesRegex(ValueError, "mode"):
            build_transmit_config(
                station_name="Example", description="", frequency="7.250 MHz",
                mode="FM", station_dbm=50, allow_overlap=False,
                auth_token="test-token",
            )

    def test_audio_url_uses_same_authenticated_session_values(self):
        url = build_audio_socket_url("session-id", "secret-value", "token-value")
        parsed = urlparse(url)
        self.assertEqual(f"{parsed.scheme}://{parsed.netloc}{parsed.path}", AUDIO_URL)
        query = parse_qs(parsed.query)
        self.assertEqual(query["session"], ["session-id"])
        self.assertEqual(query["sessionSecret"], ["secret-value"])
        self.assertEqual(query["authToken"], ["token-value"])

    def test_audio_url_requires_all_credentials(self):
        with self.assertRaises(ValueError):
            build_audio_socket_url("session-id", "", "token-value")

    def test_streaming_resampler_keeps_48khz_output_rate(self):
        import numpy as np

        resampler = StreamingLinearResampler(44_100, 48_000)
        chunks = [
            resampler.process(np.ones(512, dtype=np.float32))
            for _ in range(20)
        ]
        output = np.concatenate(chunks)
        expected = round(20 * 512 * 48_000 / 44_100)

        self.assertLessEqual(abs(len(output) - expected), 2)
        self.assertTrue(np.allclose(output, 1.0, atol=1e-6))

    def test_equal_rate_resampler_preserves_input_samples(self):
        import numpy as np

        samples = np.linspace(-1.0, 1.0, 512, dtype=np.float32)
        resampler = StreamingLinearResampler(48_000, 48_000)
        np.testing.assert_array_equal(resampler.process(samples), samples)



if __name__ == "__main__":
    unittest.main()
