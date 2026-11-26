import queue
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlparse

from transmitter import (
    AUDIO_URL,
    FRAME_BYTES,
    SAMPLE_RATE_HZ,
    BATCH_SAMPLES,
    AUDIO_FRAME_INTERVAL_SECONDS,
    PLAYLIST_AUDIO_QUEUE_FRAMES,
    PLAYLIST_PREROLL_FRAMES,
    DRM_PREROLL_FRAMES,
    SUPPORTED_MODES,
    SUPPORTED_AUDIO_SOURCES,
    LIVE_AUDIO_SILENCE_GRACE_SECONDS,
    RECONNECT_INITIAL_DELAY_SECONDS,
    RECONNECT_MAX_DELAY_SECONDS,
    reconnect_delay,
    begin_windows_system_awake,
    end_windows_system_awake,
    should_send_live_audio_silence,
    next_audio_frame_deadline,
    playlist_frame_wait_seconds,
    unpack_queued_audio_frame,
    build_audio_socket_url,
    build_transmit_config,
    apply_nfm_transport,
    apply_rade_transport,
    TxityTransmitter,
    nfm_transport_settings,
    rade_transport_settings,
    parse_rf_frequency,
    StreamingLinearResampler,
    find_frequency_overlaps,
    build_frequency_candidates,
    mode_bandwidth_hz,
    list_audio_inputs,
    list_loopback_devices,
)


class TransmitterProtocolTests(unittest.TestCase):

    def test_control_send_timeout_does_not_block_transmitter(self):
        release = threading.Event()

        class BlockedClient:
            def send_control_message(self, _message):
                release.wait(1.0)

        from transmitter import send_control_message_bounded

        started = time.monotonic()
        try:
            with self.assertRaises(TimeoutError):
                send_control_message_bounded(
                    BlockedClient(),
                    {"type": "ptt", "ptt": False},
                    timeout=0.15,
                )
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            release.set()

    def test_early_worker_failure_cannot_leave_transmitter_busy(self):
        events = queue.Queue()
        transmitter = TxityTransmitter(object(), events)
        transmitter._starting = True
        transmitter._thread = threading.current_thread()

        with mock.patch.object(transmitter, "_run", side_effect=RuntimeError("startup failed")):
            transmitter._run_guarded()

        self.assertFalse(transmitter.is_busy)
        self.assertIsNone(transmitter._thread)
        queued = [events.get_nowait(), events.get_nowait()]
        self.assertEqual(queued[0]["kind"], "tx_error")
        self.assertEqual(queued[1]["state"], "stopped")

    def test_ptt_sent_reflects_worker_state_and_stop_request(self):
        transmitter = TxityTransmitter(object(), queue.Queue())
        transmitter._active = True
        transmitter._ptt_sent = True
        self.assertTrue(transmitter.ptt_sent)

        transmitter.stop()
        self.assertFalse(transmitter.ptt_sent)
        self.assertTrue(transmitter.is_busy)

    def test_icecast_only_start_does_not_require_txity_credentials(self):
        events = queue.Queue()
        transmitter = TxityTransmitter(object(), events)
        fake_thread = mock.Mock()
        with mock.patch("transmitter.threading.Thread", return_value=fake_thread) as thread_factory:
            transmitter.start(
                {"mode": "AM", "_txitpyMode": "AM"},
                audio_source="mic",
                icecast_server_settings={
                    "enabled": True,
                    "audio_format": "ogg_opus",
                    "bitrate_kbps": 96,
                },
                txity_enabled=False,
            )

        thread_args = thread_factory.call_args.kwargs["args"]
        self.assertFalse(thread_args[-1])
        fake_thread.start.assert_called_once()

    def test_start_rejects_when_no_output_provider_is_enabled(self):
        transmitter = TxityTransmitter(object(), queue.Queue())
        with self.assertRaisesRegex(ValueError, "Enable TXITY"):
            transmitter.start(
                {"mode": "AM", "_txitpyMode": "AM"},
                txity_enabled=False,
            )
    def test_linux_loopback_lists_pulseaudio_and_pipewire_monitor_sources(self):
        devices = [
            {"name": "Built-in Audio Analog Stereo", "max_input_channels": 2, "hostapi": 0},
            {"name": "Monitor of Built-in Audio Analog Stereo", "max_input_channels": 2, "hostapi": 0},
            {"name": "alsa_output.usb.monitor", "max_input_channels": 2, "hostapi": 1},
            {"name": "Output Only Device", "max_input_channels": 0, "hostapi": 1},
        ]
        sounddevice = SimpleNamespace(
            query_devices=lambda: devices,
            query_hostapis=lambda: [{"name": "PulseAudio"}, {"name": "PipeWire"}],
        )
        with (
            mock.patch("transmitter.platform.system", return_value="Linux"),
            mock.patch.dict("sys.modules", {"sounddevice": sounddevice}),
        ):
            result = list_loopback_devices()

        self.assertEqual([item[0] for item in result], [1, 2])
        self.assertIn("[PulseAudio]", result[0][1])
        self.assertIn("[PipeWire]", result[1][1])

    def test_linux_microphone_list_excludes_monitor_sources(self):
        devices = [
            {"name": "USB Microphone", "max_input_channels": 1, "hostapi": 0},
            {"name": "Monitor of Built-in Audio", "max_input_channels": 2, "hostapi": 0},
        ]
        sounddevice = SimpleNamespace(query_devices=lambda: devices)
        with (
            mock.patch("transmitter.platform.system", return_value="Linux"),
            mock.patch.dict("sys.modules", {"sounddevice": sounddevice}),
        ):
            result = list_audio_inputs()

        self.assertEqual(result, [(0, "USB Microphone")])

    def test_audio_batch_matches_browser_worklet(self):
        self.assertEqual(BATCH_SAMPLES, 512)
        self.assertEqual(SAMPLE_RATE_HZ, 48_000)
        self.assertEqual(FRAME_BYTES, 2_048)

    def test_playlist_frames_are_paced_at_48khz(self):
        self.assertAlmostEqual(AUDIO_FRAME_INTERVAL_SECONDS, 512 / 48_000)
        first_deadline = next_audio_frame_deadline(None, 10.0)
        self.assertAlmostEqual(
            playlist_frame_wait_seconds(first_deadline, 10.0),
            AUDIO_FRAME_INTERVAL_SECONDS,
        )
        self.assertAlmostEqual(
            playlist_frame_wait_seconds(first_deadline, 10.003),
            AUDIO_FRAME_INTERVAL_SECONDS - 0.003,
        )
        self.assertEqual(playlist_frame_wait_seconds(first_deadline, 10.02), 0.0)

    def test_audio_deadline_advances_without_scheduler_drift(self):
        first_deadline = next_audio_frame_deadline(None, 10.0)
        second_deadline = next_audio_frame_deadline(
            first_deadline,
            first_deadline + 0.001,
        )
        self.assertAlmostEqual(
            second_deadline,
            first_deadline + AUDIO_FRAME_INTERVAL_SECONDS,
        )

    def test_playlist_deadline_skips_a_missed_slot(self):
        first_deadline = next_audio_frame_deadline(None, 10.0)
        late_now = first_deadline + AUDIO_FRAME_INTERVAL_SECONDS + 0.05
        next_deadline = next_audio_frame_deadline(first_deadline, late_now)
        self.assertAlmostEqual(
            next_deadline,
            late_now + AUDIO_FRAME_INTERVAL_SECONDS,
        )

    def test_drm_deadline_catches_up_after_a_late_send(self):
        first_deadline = next_audio_frame_deadline(None, 10.0)
        late_now = first_deadline + 2 * AUDIO_FRAME_INTERVAL_SECONDS + 0.05
        next_deadline = next_audio_frame_deadline(
            first_deadline,
            late_now,
            catch_up_after_delay=True,
        )
        self.assertEqual(next_deadline, late_now)
        self.assertEqual(playlist_frame_wait_seconds(next_deadline, late_now), 0.0)

        following_deadline = next_audio_frame_deadline(
            next_deadline,
            late_now,
            catch_up_after_delay=True,
        )
        self.assertAlmostEqual(
            following_deadline,
            late_now + AUDIO_FRAME_INTERVAL_SECONDS,
        )

    def test_playlist_buffer_stays_a_few_seconds_ahead(self):
        self.assertGreater(PLAYLIST_AUDIO_QUEUE_FRAMES, PLAYLIST_PREROLL_FRAMES)
        self.assertEqual(PLAYLIST_AUDIO_QUEUE_FRAMES, 512)
        self.assertEqual(PLAYLIST_PREROLL_FRAMES, 256)
        self.assertAlmostEqual(
            PLAYLIST_AUDIO_QUEUE_FRAMES * AUDIO_FRAME_INTERVAL_SECONDS,
            5.461333333333333,
        )
        self.assertAlmostEqual(
            PLAYLIST_PREROLL_FRAMES * AUDIO_FRAME_INTERVAL_SECONDS,
            2.7306666666666666,
        )

    def test_unpacks_drm_playlist_audio_before_websocket_send(self):
        frame = bytes(FRAME_BYTES)
        metadata = None

        payload, playlist_info = unpack_queued_audio_frame((frame, metadata))

        self.assertIs(payload, frame)
        self.assertIsNone(playlist_info)
        self.assertIsInstance(payload, bytes)
        self.assertEqual(len(payload), FRAME_BYTES)

    def test_unpacks_legacy_and_rade_playlist_queue_items(self):
        frame = bytes(FRAME_BYTES)
        playlist_info = ("track.ogg", 1, 2)

        payload, info = unpack_queued_audio_frame((frame, playlist_info))
        self.assertIs(payload, frame)
        self.assertEqual(info, playlist_info)

        payload, info = unpack_queued_audio_frame(frame)
        self.assertIs(payload, frame)
        self.assertIsNone(info)

    def test_live_audio_silence_only_fills_sustained_input_gaps(self):
        self.assertEqual(LIVE_AUDIO_SILENCE_GRACE_SECONDS, 0.1)
        self.assertFalse(should_send_live_audio_silence(10.0, 10.05))
        self.assertTrue(should_send_live_audio_silence(10.0, 10.101))

    def test_reconnect_delay_grows_then_stays_bounded(self):
        self.assertEqual(RECONNECT_INITIAL_DELAY_SECONDS, 1.0)
        self.assertEqual(RECONNECT_MAX_DELAY_SECONDS, 15.0)
        self.assertEqual(
            [reconnect_delay(attempt) for attempt in range(1, 8)],
            [1.0, 2.0, 4.0, 8.0, 15.0, 15.0, 15.0],
        )

    def test_windows_awake_request_cleanup_resets_execution_state(self):
        states = []
        end_windows_system_awake((None, states.append))
        self.assertEqual(states, [0x80000000])

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

    def test_builds_ordered_unique_frequency_fallback_candidates(self):
        self.assertEqual(
            build_frequency_candidates(
                "7.250 MHz",
                ["7250 kHz", "7.300 MHz", "", "7.350 MHz"],
            ),
            [
                ("7.250 MHz", 7_250_000),
                ("7.300 MHz", 7_300_000),
                ("7.350 MHz", 7_350_000),
            ],
        )

    def test_frequency_candidates_reject_invalid_fallbacks(self):
        with self.assertRaises(ValueError):
            build_frequency_candidates("7.250 MHz", ["not a frequency"])
        with self.assertRaises(ValueError):
            build_frequency_candidates("", ["7.300 MHz"])

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
        with self.assertRaisesRegex(ValueError, "station name"):
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

    def test_audio_sources_are_limited_to_supported_inputs(self):
        self.assertIn("DRM", SUPPORTED_MODES)
        self.assertIn("NFM", SUPPORTED_MODES)
        self.assertEqual(SUPPORTED_AUDIO_SOURCES, ("mic", "loopback", "playlist"))

    def test_drm_mode_uses_one_dsb_transport_channel(self):
        self.assertIn("DRM", SUPPORTED_MODES)
        self.assertEqual(DRM_PREROLL_FRAMES, 94)
        config = build_transmit_config(
            station_name="Example DRM",
            description="Single-service DRM test",
            frequency="7.250 MHz",
            mode="DRM",
            station_dbm=40,
            allow_overlap=False,
            auth_token="test-token",
            am_bandwidth_hz=9_000,
            am_pre_emphasis=True,
            fade_enabled=True,
            agc_enabled=True,
            mic_gain_db=3,
            bass_db=2,
            treble_db=-2,
            presence_db=1,
            drive=0.02,
            drm_service_label="BLABS Radio",
            drm_service_id="0x123ABC",
            drm_language="English",
            drm_programme_type="Rock Music",
            drm_iso_language="eng",
            drm_iso_country="US",
            drm_audio_codec="HE-AAC",
            drm_interleaving="short",
        )
        self.assertEqual(config["_txitpyDrmAudioCodec"], "HE-AAC")
        self.assertEqual(config["_txitpyDrmInterleaving"], "short")
        self.assertEqual(config["_txitpyDrmServiceLabel"], "BLABS Radio")
        self.assertEqual(config["_txitpyDrmServiceId"], "0x123ABC")
        self.assertEqual(config["_txitpyDrmLanguage"], "English")
        self.assertEqual(config["_txitpyDrmProgrammeType"], "Rock Music")
        self.assertEqual(config["_txitpyDrmIsoLanguage"], "eng")
        self.assertEqual(config["_txitpyDrmIsoCountry"], "US")
        self.assertEqual(config["mode"], "DSB")
        self.assertEqual(config["_txitpyMode"], "DRM")
        self.assertEqual(config["amBandwidthHz"], 12_000)
        self.assertFalse(config["amPreEmphasis"])
        self.assertEqual(config["fadeMs"], 0)
        self.assertFalse(config["agcEnabled"])
        self.assertEqual(config["micGainDb"], 0)
        self.assertEqual(config["bassDb"], 0)
        self.assertEqual(config["trebleDb"], 0)
        self.assertEqual(config["presenceDb"], 0)
        self.assertEqual(config["drive"], 0)

    def test_drm_metadata_defaults_are_available_without_custom_values(self):
        config = build_transmit_config(
            station_name="Example DRM",
            description="",
            frequency="7 MHz",
            mode="DRM",
            station_dbm=40,
            allow_overlap=False,
            auth_token="test-token",
        )
        self.assertEqual(config["_txitpyDrmServiceLabel"], "")
        self.assertEqual(config["_txitpyDrmServiceId"], "0xB30001")
        self.assertEqual(config["_txitpyDrmLanguage"], "English")
        self.assertEqual(config["_txitpyDrmProgrammeType"], "No programme type")
        self.assertEqual(config["_txitpyDrmIsoLanguage"], "")
        self.assertEqual(config["_txitpyDrmIsoCountry"], "")
        self.assertEqual(config["_txitpyDrmAudioCodec"], "xHE-AAC")
        self.assertEqual(config["_txitpyDrmInterleaving"], "long")

    def test_removed_am_dev_mode_is_rejected(self):
        self.assertNotIn("AM (DEV)", SUPPORTED_MODES)
        with self.assertRaisesRegex(ValueError, "Unsupported transmission mode"):
            build_transmit_config(
                station_name="Example Radio",
                description="Removed test mode",
                frequency="7.250 MHz",
                mode="AM (DEV)",
                station_dbm=40,
                allow_overlap=False,
                auth_token="test-token",
            )


    def test_nfm_mode_uses_a_frequency_translated_ssb_transport(self):
        config = build_transmit_config(
            station_name="Example NFM",
            description="NFM test",
            frequency="7.250 MHz",
            mode="NFM",
            station_dbm=40,
            allow_overlap=False,
            auth_token="test-token",
            mic_gain_db=3,
            agc_enabled=True,
            drive=0.02,
        )

        self.assertEqual(config["mode"], "USB")
        self.assertEqual(config["rfHz"], 7_245_000)
        self.assertEqual(config["_txitpyMode"], "NFM")
        self.assertEqual(config["_txitpyNfmIfHz"], 5_000)
        self.assertEqual(config["_txitpyNfmDeviationHz"], 2_500)
        self.assertEqual(config["_txitpyNfmInputGainDb"], 3)
        self.assertEqual(config["_txitpyNfmSideband"], "USB")
        self.assertEqual(config["ssbBandwidthHz"], 12_000)
        self.assertEqual(config["ssbLowCutHz"], 0)
        self.assertEqual(config["ssbHighCutHz"], 0)
        self.assertFalse(config["agcEnabled"])
        self.assertEqual(config["micGainDb"], 0)
        self.assertEqual(config["drive"], 0)

    def test_wide_nfm_profile_uses_6khz_if_and_correct_frequency_translation(self):
        config = build_transmit_config(
            station_name="Example Wide NFM",
            description="Wide NFM test",
            frequency="7.250 MHz",
            mode="NFM",
            station_dbm=40,
            allow_overlap=False,
            auth_token="test-token",
            nfm_profile="Wide (11 kHz)",
        )

        self.assertEqual(config["mode"], "USB")
        self.assertEqual(config["rfHz"], 7_244_000)
        self.assertEqual(config["_txitpyNfmIfHz"], 6_000)
        self.assertEqual(config["_txitpyNfmDeviationHz"], 2_500)
        self.assertEqual(config["_txitpyNfmInputLowCutHz"], 100)
        self.assertEqual(config["_txitpyNfmInputHighCutHz"], 3_000)
        self.assertEqual(config["_txitpyNfmChannelBandwidthHz"], 11_000)
        self.assertEqual(config["ssbBandwidthHz"], 12_000)

        apply_nfm_transport(config, "7.300 MHz")
        self.assertEqual(config["rfHz"], 7_294_000)

        self.assertEqual(
            mode_bandwidth_hz("NFM", nfm_profile="Wide (11 kHz)"),
            11_000,
        )

    def test_drm_quality_profile_is_encoded_in_station_config(self):
        robust = build_transmit_config(
            station_name="Robust DRM",
            description="",
            frequency="7.250 MHz",
            mode="DRM",
            station_dbm=40,
            allow_overlap=False,
            auth_token="test-token",
        )
        maximum = build_transmit_config(
            station_name="Maximum DRM",
            description="",
            frequency="7.250 MHz",
            mode="DRM",
            station_dbm=40,
            allow_overlap=False,
            auth_token="test-token",
            drm_quality_profile="Maximum quality (9 kHz, 64-QAM stereo)",
        )

        self.assertEqual(robust["_txitpyDrmOccupancy"], 0)
        self.assertEqual(robust["_txitpyDrmMscMode"], "16-QAM")
        self.assertFalse(robust["_txitpyDrmStereo"])
        self.assertEqual(maximum["_txitpyDrmOccupancy"], 2)
        self.assertEqual(maximum["_txitpyDrmMscMode"], "64-QAM")
        self.assertTrue(maximum["_txitpyDrmStereo"])

    def test_nfm_transport_bandwidth_accepts_one_to_twenty_khz(self):
        for bandwidth_hz in (1_000, 5_000, 12_000, 20_000):
            with self.subTest(bandwidth_hz=bandwidth_hz):
                config = build_transmit_config(
                    station_name="Example NFM",
                    description="NFM bandwidth test",
                    frequency="7.250 MHz",
                    mode="NFM",
                    station_dbm=40,
                    allow_overlap=False,
                    auth_token="test-token",
                    nfm_transport_bandwidth_hz=bandwidth_hz,
                )
                self.assertEqual(config["ssbBandwidthHz"], bandwidth_hz)

        for bandwidth_hz in (999, 20_001):
            with self.subTest(invalid_bandwidth_hz=bandwidth_hz):
                with self.assertRaisesRegex(ValueError, "NFM transport bandwidth"):
                    build_transmit_config(
                        station_name="Example NFM",
                        description="NFM bandwidth test",
                        frequency="7.250 MHz",
                        mode="NFM",
                        station_dbm=40,
                        allow_overlap=False,
                        auth_token="test-token",
                        nfm_transport_bandwidth_hz=bandwidth_hz,
                    )

    def test_nfm_transport_handles_edges_of_the_available_room(self):
        self.assertEqual(nfm_transport_settings("7.000 MHz"), ("LSB", 7_005_000))
        self.assertEqual(nfm_transport_settings("8.000 MHz"), ("USB", 7_995_000))

        config = {"_txitpyMode": "NFM", "mode": "USB"}
        apply_nfm_transport(config, 7_000_000)
        self.assertEqual(config["mode"], "LSB")
        self.assertEqual(config["rfHz"], 7_005_000)

        with self.assertRaises(ValueError):
            nfm_transport_settings("6.950 MHz")

    def test_drm_mode_uses_standard_mode_name(self):
        config = build_transmit_config(
            station_name="Example DRM",
            description="",
            frequency="7 MHz",
            mode="DRM",
            station_dbm=40,
            allow_overlap=False,
            auth_token="test-token",
        )

        self.assertEqual(config["_txitpyMode"], "DRM")
        self.assertEqual(config["mode"], "DSB")

    def test_rade_uses_the_requested_frequency_as_its_usb_carrier_reference(self):
        config = build_transmit_config(
            station_name="Example RADE",
            description="FreeDV RADE V1",
            frequency="7.250 MHz",
            mode="RADE",
            station_dbm=40,
            allow_overlap=False,
            auth_token="test-token",
        )

        self.assertIn("RADE", SUPPORTED_MODES)
        self.assertEqual(config["_txitpyMode"], "RADE")
        self.assertEqual(config["mode"], "USB")
        self.assertEqual(config["rfHz"], 7_250_000)
        self.assertEqual(config["_txitpyRadeCenterHz"], 1_500)
        self.assertEqual(config["_txitpyRadeChannelBandwidthHz"], 1_500)
        self.assertEqual(config["ssbBandwidthHz"], 3_000)
        self.assertFalse(config["agcEnabled"])
        self.assertEqual(config["fadeMs"], 0)
        self.assertEqual(config["micGainDb"], 0)
        self.assertEqual(config["drive"], 0)

    def test_rade_keeps_the_requested_usb_carrier_at_room_edges(self):
        self.assertEqual(rade_transport_settings("7.000 MHz"), ("USB", 7_000_000))
        self.assertEqual(rade_transport_settings("8.000 MHz"), ("USB", 8_000_000))

        config = {"_txitpyMode": "RADE", "mode": "LSB"}
        apply_rade_transport(config, "7.000 MHz")
        self.assertEqual(config["mode"], "USB")
        self.assertEqual(config["rfHz"], 7_000_000)

        with self.assertRaises(ValueError):
            rade_transport_settings("6.999 MHz")

    def test_mode_bandwidth_defaults_match_txity_transmit_config(self):
        self.assertEqual(mode_bandwidth_hz("DRM"), 12_000)
        self.assertEqual(mode_bandwidth_hz("NFM"), 9_000)
        self.assertEqual(mode_bandwidth_hz("RADE"), 1_500)
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
