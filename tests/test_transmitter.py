import unittest
from urllib.parse import parse_qs, urlparse

from transmitter import (
    AUDIO_URL,
    FRAME_BYTES,
    SAMPLE_RATE_HZ,
    BATCH_SAMPLES,
    AUDIO_FRAME_INTERVAL_SECONDS,
    PLAYLIST_AUDIO_QUEUE_FRAMES,
    PLAYLIST_PREROLL_FRAMES,
    next_playlist_frame_deadline,
    playlist_frame_wait_seconds,
    build_audio_socket_url,
    build_transmit_config,
    parse_rf_frequency,
    StreamingLinearResampler,
    find_frequency_overlaps,
    mode_bandwidth_hz,
)


class TransmitterProtocolTests(unittest.TestCase):
    def test_audio_batch_matches_browser_worklet(self):
        self.assertEqual(BATCH_SAMPLES, 512)
        self.assertEqual(SAMPLE_RATE_HZ, 48_000)
        self.assertEqual(FRAME_BYTES, 2_048)

    def test_playlist_frames_are_paced_at_48khz(self):
        self.assertAlmostEqual(AUDIO_FRAME_INTERVAL_SECONDS, 512 / 48_000)
        first_deadline = next_playlist_frame_deadline(None, 10.0)
        self.assertAlmostEqual(
            playlist_frame_wait_seconds(first_deadline, 10.0),
            AUDIO_FRAME_INTERVAL_SECONDS,
        )
        self.assertAlmostEqual(
            playlist_frame_wait_seconds(first_deadline, 10.003),
            AUDIO_FRAME_INTERVAL_SECONDS - 0.003,
        )
        self.assertEqual(playlist_frame_wait_seconds(first_deadline, 10.02), 0.0)

    def test_playlist_deadlines_do_not_accumulate_small_scheduler_delays(self):
        first_deadline = next_playlist_frame_deadline(None, 10.0)
        second_deadline = next_playlist_frame_deadline(
            first_deadline,
            first_deadline + 0.001,
        )
        self.assertAlmostEqual(
            second_deadline,
            first_deadline + AUDIO_FRAME_INTERVAL_SECONDS,
        )

    def test_playlist_deadline_resets_after_a_long_stall(self):
        first_deadline = next_playlist_frame_deadline(None, 10.0)
        late_now = first_deadline + AUDIO_FRAME_INTERVAL_SECONDS + 0.05
        next_deadline = next_playlist_frame_deadline(first_deadline, late_now)
        self.assertAlmostEqual(
            next_deadline,
            late_now + AUDIO_FRAME_INTERVAL_SECONDS,
        )

    def test_playlist_buffer_has_room_for_preroll(self):
        self.assertGreater(PLAYLIST_AUDIO_QUEUE_FRAMES, PLAYLIST_PREROLL_FRAMES)
        self.assertEqual(PLAYLIST_AUDIO_QUEUE_FRAMES, 256)
        self.assertEqual(PLAYLIST_PREROLL_FRAMES, 96)

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
        self.assertEqual(config["fadeMs"], 0)
        self.assertTrue(config["agcEnabled"])
        self.assertEqual(config["agcTarget"], 0.19)
        self.assertEqual(config["micGainDb"], 0)
        self.assertEqual(config["ssbBandwidthHz"], 10_000)
        self.assertFalse(config["cwLoop"])
        self.assertEqual(config["discordAuthToken"], "test-token")

    def test_builds_config_with_fading_and_audio_processing_options(self):
        config = build_transmit_config(
            station_name="Example Radio",
            description="Test stream",
            frequency="7.250 MHz",
            mode="USB",
            station_dbm=40,
            allow_overlap=False,
            auth_token="test-token",
            fade_enabled=True,
            fade_seconds="17.5",
            fade_min_dbm=25,
            fade_shape=3,
            agc_enabled=False,
            agc_target="0.22",
            mic_gain_db="-3",
            bass_db=2,
            treble_db=-1,
            presence_db=1,
            drive="0.02",
            ssb_bandwidth_hz=8_000,
            ssb_low_cut_hz=250,
            ssb_high_cut_hz=500,
            am_bandwidth_hz=9_000,
            am_pre_emphasis=True,
        )
        self.assertEqual(config["fadeMs"], 17_500)
        self.assertEqual(config["fadeMinDbm"], 25)
        self.assertEqual(config["fadeShape"], 3)
        self.assertFalse(config["agcEnabled"])
        self.assertEqual(config["agcTarget"], 0.22)
        self.assertEqual(config["micGainDb"], -3)
        self.assertEqual(config["bassDb"], 2)
        self.assertEqual(config["trebleDb"], -1)
        self.assertEqual(config["presenceDb"], 1)
        self.assertEqual(config["drive"], 0.02)
        self.assertEqual(config["ssbBandwidthHz"], 8_000)
        self.assertEqual(config["ssbLowCutHz"], 250)
        self.assertEqual(config["ssbHighCutHz"], 500)
        self.assertEqual(config["amBandwidthHz"], 9_000)
        self.assertTrue(config["amPreEmphasis"])

    def test_disabled_fading_sends_zero_fade_duration_and_clamps_floor(self):
        config = build_transmit_config(
            station_name="Example Radio",
            description="",
            frequency="7.250 MHz",
            mode="AM",
            station_dbm=20,
            allow_overlap=False,
            auth_token="test-token",
            fade_enabled=False,
            fade_seconds=15,
            fade_min_dbm=45,
        )
        self.assertEqual(config["fadeMs"], 0)
        self.assertEqual(config["fadeMinDbm"], 20)

    def test_rejects_out_of_range_filter_values(self):
        invalid_settings = (
            {"agc_target": 0.3},
            {"mic_gain_db": 7},
            {"bass_db": -5},
            {"presence_db": 2},
            {"drive": 0.03},
            {"ssb_bandwidth_hz": 2_000},
            {"am_bandwidth_hz": 13_000},
            {"fade_seconds": 5},
            {"fade_shape": 6},
        )
        for setting in invalid_settings:
            with self.subTest(setting=setting):
                with self.assertRaises(ValueError):
                    build_transmit_config(
                        station_name="Example Radio",
                        description="",
                        frequency="7.250 MHz",
                        mode="AM",
                        station_dbm=40,
                        allow_overlap=False,
                        auth_token="test-token",
                        **setting,
                    )

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

    def test_mode_bandwidth_defaults_match_txity_transmit_config(self):
        self.assertEqual(mode_bandwidth_hz("AM"), 12_000)
        self.assertEqual(mode_bandwidth_hz("DSB"), 12_000)
        self.assertEqual(mode_bandwidth_hz("USB"), 10_000)
        self.assertEqual(mode_bandwidth_hz("LSB+C"), 10_000)
        self.assertEqual(mode_bandwidth_hz("CW"), 500)

    def test_detects_overlapping_virtual_station_frequency(self):
        overlaps = find_frequency_overlaps(
            "7.250 MHz",
            "AM",
            [{
                "id": "other",
                "station": "Nearby station",
                "frequency_hz": 7_245_000,
                "mode": "AM",
                "allow_overlap_visitors": True,
            }],
        )
        self.assertEqual(len(overlaps), 1)
        self.assertEqual(overlaps[0]["station_name"], "Nearby station")
        self.assertEqual(overlaps[0]["offset_hz"], 5_000)
        self.assertEqual(overlaps[0]["overlap_hz"], 7_000)
        self.assertTrue(overlaps[0]["allow_overlap_visitors"])

    def test_ignores_non_overlapping_or_unreported_station_frequencies(self):
        overlaps = find_frequency_overlaps(
            "7.250 MHz",
            "AM",
            [
                {"station": "Far station", "frequency": "7.300 MHz", "mode": "AM"},
                {"station": "No frequency", "frequency": "Not reported", "mode": "USB"},
            ],
        )
        self.assertEqual(overlaps, [])

    def test_station_reported_bandwidth_overrides_mode_default(self):
        self.assertEqual(mode_bandwidth_hz("AM", {"bandwidthHz": 2_400}), 2_400)



if __name__ == "__main__":
    unittest.main()
