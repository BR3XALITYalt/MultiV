import logging
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from cli import _handle_event, _read_auth_token, build_argument_parser, build_headless_plan


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self.parser = build_argument_parser()

    def test_parser_accepts_headless_service_options(self):
        args = self.parser.parse_args([
            "--nogui",
            "--mode", "drm",
            "--audio-source", "playlist",
            "--playlist", "D:/Radio/One.ogg",
            "--no-loop",
            "--no-shuffle",
            "--auth-token-file", "C:/ProgramData/TXITPY/token.txt",
            "--frequency", "7 MHz",
            "--frequency-fallback", "7.2 MHz",
            "--frequency-fallback", "7.25 MHz",
            "--frequency-fallback", "7.5 MHz",
        ])
        self.assertTrue(args.no_gui)
        self.assertEqual(args.mode, "DRM")
        self.assertEqual(args.audio_source, "playlist")
        self.assertFalse(args.loop)
        self.assertFalse(args.shuffle)
        self.assertEqual(args.auth_token_file, Path("C:/ProgramData/TXITPY/token.txt"))
        self.assertEqual(args.frequency, "7 MHz")
        self.assertEqual(args.frequency_fallback, ["7.2 MHz", "7.25 MHz", "7.5 MHz"])

    def test_headless_reads_saved_appdata_token_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "TXITPY" / "token.txt"
            token_file.parent.mkdir(parents=True)
            token_file.write_text("saved-token-for-headless-test", encoding="utf-8")
            args = self.parser.parse_args(["--no-gui"])
            with patch("cli.auth_token_path", return_value=token_file):
                self.assertEqual(
                    _read_auth_token(args),
                    "saved-token-for-headless-test",
                )

    def test_missing_service_token_override_falls_back_to_appdata(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "TXITPY" / "token.txt"
            token_file.parent.mkdir(parents=True)
            token_file.write_text("saved-token-for-headless-test", encoding="utf-8")
            args = self.parser.parse_args([
                "--no-gui",
                "--auth-token-file",
                "C:/ProgramData/TXITPY/discord-token.txt",
            ])
            with patch("cli.auth_token_path", return_value=token_file):
                self.assertEqual(
                    _read_auth_token(args),
                    "saved-token-for-headless-test",
                )

    def test_saved_settings_are_used_when_no_cli_override_is_given(self):
        with tempfile.TemporaryDirectory() as directory:
            audio_file = Path(directory) / "radio.ogg"
            audio_file.write_bytes(b"not decoded during planning")
            settings = {
                "station_name": "Example Radio",
                "frequency": "7.250 MHz",
                "mode": "AM",
                "audio_source": "Audio playlist",
                "playlist_sources": [str(audio_file)],
                "playlist_loop": True,
                "allow_overlap": True,
                "station_dbm": "42",
            }
            args = self.parser.parse_args(["--no-gui"])
            plan = build_headless_plan(settings, args, "test-token")

        self.assertEqual(plan["config"]["stationName"], "Example Radio")
        self.assertEqual(plan["config"]["rfHz"], 7_250_000)
        self.assertEqual(
            plan["frequency_candidates"],
            [("7.250 MHz", 7_250_000)],
        )
        self.assertEqual(plan["config"]["stationDbm"], 42)
        self.assertEqual(plan["audio_source"], "playlist")
        self.assertTrue(plan["playlist_loop"])
        self.assertTrue(plan["config"]["allowOverlapVisitors"])
        self.assertEqual(len(plan["playlist_files"]), 1)

    def test_cli_values_override_saved_settings(self):
        args = self.parser.parse_args([
            "--no-gui",
            "--station-name", "CLI Radio",
            "--frequency", "14.200 MHz",
            "--mode", "USB",
            "--power", "30",
            "--audio-source", "mic",
            "--no-allow-overlap",
        ])
        plan = build_headless_plan({
            "station_name": "Saved Radio",
            "frequency": "7.250 MHz",
            "mode": "AM",
            "station_dbm": "50",
            "allow_overlap": True,
        }, args, "test-token")

        self.assertEqual(plan["config"]["stationName"], "CLI Radio")
        self.assertEqual(plan["config"]["rfHz"], 14_200_000)
        self.assertEqual(plan["config"]["mode"], "USB")
        self.assertEqual(plan["config"]["stationDbm"], 30)
        self.assertFalse(plan["config"]["allowOverlapVisitors"])
        self.assertEqual(plan["audio_source"], "mic")

    def test_cli_frequency_and_fallback_arguments_preserve_retry_order(self):
        args = self.parser.parse_args([
            "--no-gui",
            "--frequency", "7 MHz",
            "--frequency-fallback", "7.2 MHz",
            "--frequency-fallback", "7.25 MHz",
            "--frequency-fallback", "7.5 MHz",
        ])
        plan = build_headless_plan(
            {
                "station_name": "Frequency Test",
                "frequency": "14.200 MHz",
                "frequency_fallbacks": ["14.300 MHz"],
            },
            args,
            "test-token",
        )
        self.assertEqual(
            plan["frequency_candidates"],
            [
                ("7 MHz", 7_000_000),
                ("7.2 MHz", 7_200_000),
                ("7.25 MHz", 7_250_000),
                ("7.5 MHz", 7_500_000),
            ],
        )

    def test_saved_frequency_fallbacks_are_used_in_headless_mode(self):
        args = self.parser.parse_args(["--no-gui"])
        plan = build_headless_plan(
            {
                "station_name": "Fallback Radio",
                "frequency": "7.250 MHz",
                "frequency_fallbacks": ["7.300 MHz", "7.350 MHz"],
            },
            args,
            "test-token",
        )
        self.assertEqual(
            plan["frequency_candidates"],
            [
                ("7.250 MHz", 7_250_000),
                ("7.300 MHz", 7_300_000),
                ("7.350 MHz", 7_350_000),
            ],
        )

    def test_cli_frequency_override_stays_first_before_saved_fallbacks(self):
        args = self.parser.parse_args(["--no-gui", "--frequency", "14.200 MHz"])
        plan = build_headless_plan(
            {
                "station_name": "Override Radio",
                "frequency": "7.250 MHz",
                "frequency_fallbacks": ["7.300 MHz"],
            },
            args,
            "test-token",
        )
        self.assertEqual(
            plan["frequency_candidates"],
            [
                ("14.200 MHz", 14_200_000),
                ("7.300 MHz", 7_300_000),
            ],
        )

    def test_headless_mode_retries_frequencies_after_server_sharing_denial(self):
        class FakeTransmitter:
            server_rejection_ready = True

            def __init__(self):
                self.is_busy = True
                self.stop_calls = 0
                self.starts = []

            def stop(self):
                self.stop_calls += 1
                self.is_busy = False

            def start(self, config, **options):
                self.is_busy = True
                self.starts.append((dict(config), options))

        transmitter = FakeTransmitter()
        shutdown_event = threading.Event()
        state = {
            "exit_code": 0,
            "negative_reason": None,
            "negative_count": 0,
            "ptt_confirmed": False,
            "frequency_index": 0,
            "retry_pending": False,
            "retry_candidate_index": 0,
        }
        plan = {
            "config": {"rfHz": 7_250_000},
            "base_config": {"rfHz": 7_250_000},
            "frequency_candidates": [
                ("7.250 MHz", 7_250_000),
                ("7.300 MHz", 7_300_000),
            ],
            "input_device": None,
            "audio_source": "mic",
            "playlist_files": [],
            "playlist_shuffle": False,
            "playlist_loop": False,
        }
        denial = {
            "kind": "status",
            "data": {"ptt": False, "txStartUsable": False, "txStartReason": 3},
        }
        common = {
            "logger": logging.getLogger("test"),
            "transmitter": transmitter,
            "shutdown_event": shutdown_event,
            "state": state,
            "plan": plan,
        }

        _handle_event(denial, **common)
        self.assertEqual(state["negative_count"], 1)
        _handle_event(denial, **common)
        self.assertTrue(state["retry_pending"])
        self.assertEqual(transmitter.stop_calls, 1)
        self.assertFalse(shutdown_event.is_set())

        transmitter.is_busy = False
        _handle_event({"kind": "tx_state", "state": "stopped"}, **common)
        self.assertFalse(state["retry_pending"])
        self.assertEqual(state["frequency_index"], 1)
        self.assertEqual(transmitter.starts[0][0]["rfHz"], 7_300_000)
        self.assertFalse(shutdown_event.is_set())

    def test_headless_mode_does_not_change_frequency_for_capacity_denial(self):
        class FakeTransmitter:
            server_rejection_ready = True
            is_busy = True

            def __init__(self):
                self.stop_calls = 0

            def stop(self):
                self.stop_calls += 1

        transmitter = FakeTransmitter()
        shutdown_event = threading.Event()
        state = {
            "exit_code": 0,
            "negative_reason": None,
            "negative_count": 0,
            "ptt_confirmed": False,
            "frequency_index": 0,
            "retry_pending": False,
            "retry_candidate_index": 0,
        }
        plan = {
            "frequency_candidates": [
                ("7.250 MHz", 7_250_000),
                ("7.300 MHz", 7_300_000),
            ],
        }
        event = {
            "kind": "status",
            "data": {"ptt": False, "txStartUsable": False, "txStartReason": 2},
        }
        common = {
            "logger": __import__("logging").getLogger("test"),
            "transmitter": transmitter,
            "shutdown_event": shutdown_event,
            "state": state,
            "plan": plan,
        }
        _handle_event(event, **common)
        _handle_event(event, **common)
        self.assertFalse(state["retry_pending"])
        self.assertTrue(shutdown_event.is_set())
        self.assertEqual(transmitter.stop_calls, 1)

    def test_device_can_be_selected_by_name_or_numeric_id(self):
        devices = [(2, "USB Audio Codec"), (5, "Built-in Microphone")]
        args = self.parser.parse_args([
            "--no-gui",
            "--audio-source", "mic",
            "--audio-device", "USB Audio",
        ])
        plan = build_headless_plan(
            {"station_name": "Device Test", "audio_device": "System default"},
            args,
            "test-token",
            input_devices=devices,
        )
        self.assertEqual(plan["input_device"], 2)
        self.assertEqual(plan["audio_device_name"], "USB Audio Codec")

        args = self.parser.parse_args([
            "--no-gui",
            "--audio-source", "mic",
            "--audio-device", "5",
        ])
        plan = build_headless_plan(
            {"station_name": "Device Test"},
            args,
            "test-token",
            input_devices=devices,
        )
        self.assertEqual(plan["input_device"], 5)

    def test_loopback_defaults_to_the_default_windows_output(self):
        args = self.parser.parse_args([
            "--no-gui",
            "--audio-source", "loopback",
        ])
        plan = build_headless_plan(
            {"station_name": "Loopback Test"},
            args,
            "test-token",
            loopback_devices=[
                (7, "7: Other Output"),
                (9, "9: Main Speakers (Default output)"),
            ],
        )
        self.assertEqual(plan["input_device"], 9)
        self.assertIn("Default output", plan["audio_device_name"])

    def test_drm_rejects_loopback_input(self):
        args = self.parser.parse_args([
            "--no-gui",
            "--mode", "DRM",
            "--audio-source", "loopback",
        ])
        with self.assertRaisesRegex(ValueError, "DRM mode supports"):
            build_headless_plan(
                {"station_name": "DRM Test"},
                args,
                "test-token",
                loopback_devices=[(0, "0: Main Speakers (Default output)")],
            )


if __name__ == "__main__":
    unittest.main()
