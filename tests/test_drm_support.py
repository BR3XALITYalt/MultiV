import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

from drm_support import (
    DECDRM_COMMIT,
    DECDRM_VERSION,
    DRM_PLAYLIST_EXTENSIONS,
    VISUAL_STUDIO_BUILD_TOOLS_LABEL,
    VISUAL_STUDIO_BUILD_TOOLS_URL,
    VISUAL_STUDIO_WORKLOAD,
    LocalDRMPlaylistServer,
    _patch_decdrm_output_validation,
    _run_command,
    write_drm_station_config,
)


class DRMConfigurationTests(unittest.TestCase):
    def test_build_command_streams_output_to_progress_callback(self):
        updates = []
        output = _run_command(
            [
                sys.executable,
                "-c",
                "print('Compiling station encoder'); print('Finished release build')",
            ],
            on_log=updates.append,
        )

        self.assertEqual(
            updates,
            ["Compiling station encoder", "Finished release build"],
        )
        self.assertIn("Finished release build", output)

    def test_windows_build_tools_installer_uses_cpp_desktop_workload(self):
        self.assertEqual(
            VISUAL_STUDIO_BUILD_TOOLS_URL,
            "https://aka.ms/vs/stable/vs_buildtools.exe",
        )
        self.assertEqual(
            VISUAL_STUDIO_WORKLOAD,
            "Microsoft.VisualStudio.Workload.VCTools",
        )
        self.assertIn("Desktop development with C++", VISUAL_STUDIO_BUILD_TOOLS_LABEL)

    def test_decdrm_source_is_pinned(self):
        self.assertEqual(DECDRM_VERSION, "0.7.1")
        self.assertRegex(DECDRM_COMMIT, r"^[0-9a-f]{40}$")

    def test_drm_playlist_formats_are_explicit(self):
        self.assertEqual(DRM_PLAYLIST_EXTENSIONS, {".ogg", ".mp3", ".flac"})

    def test_writes_one_mono_he_aac_service_in_4_5khz_channel(self):
        with tempfile.TemporaryDirectory() as folder:
            path = write_drm_station_config(
                Path(folder) / "station.toml",
                station_name="Example DRM station",
                playlist_url="http://127.0.0.1:12345/playlist.m3u",
            )
            config = tomllib.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(config["channel"]["mode"], "B")
        self.assertEqual(config["channel"]["occupancy"], 0)
        self.assertEqual(config["output"]["sample_format"], "float32")
        self.assertEqual(config["output"]["format"], "real")
        self.assertEqual(config["output"]["if_hz"], 1500.0)
        self.assertEqual(len(config["service"]), 1)
        service = config["service"][0]
        self.assertEqual(service["audio"]["codec"], "he-aac")
        self.assertEqual(service["audio"]["core_rate"], 12000)
        self.assertFalse(service["audio"]["stereo"])
        self.assertEqual(
            service["audio"]["input"]["url"],
            "http://127.0.0.1:12345/playlist.m3u",
        )

    def test_writes_microphone_device_input(self):
        with tempfile.TemporaryDirectory() as folder:
            path = write_drm_station_config(
                Path(folder) / "station.toml",
                station_name="Mic",
                input_device_name="USB Microphone",
            )
            config = tomllib.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(
            config["service"][0]["audio"]["input"]["device"],
            "USB Microphone",
        )
        self.assertNotIn("url", config["service"][0]["audio"]["input"])

    def test_requires_exactly_one_audio_input(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "station.toml"
            with self.assertRaises(ValueError):
                write_drm_station_config(path, station_name="Test")
            with self.assertRaises(ValueError):
                write_drm_station_config(
                    path,
                    station_name="Test",
                    input_device_name="default",
                    playlist_url="http://127.0.0.1/playlist.m3u",
                )

    def test_decdrm_output_patch_is_exact_and_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "plan.rs"
            source = (
                "fn check_output() {\n"
                "    if out.file.is_none() && out.device.is_none() {\n"
                + '        p.push("output: set ' + chr(96) + 'file' + chr(96)
                + ' and/or ' + chr(96) + 'device' + chr(96) + '");\n'
                + "    }\n"
                "}\n"
            )
            path.write_text(source, encoding="utf-8")

            self.assertTrue(_patch_decdrm_output_validation(path))
            patched = path.read_text(encoding="utf-8")
            self.assertIn(
                "TXITPY consumes the samples returned by Station::transmit_frame()",
                patched,
            )
            self.assertNotIn("out.file.is_none()", patched)
            self.assertFalse(_patch_decdrm_output_validation(path))


class DRMPlaylistServerTests(unittest.TestCase):
    def test_serves_each_playlist_file_without_loading_the_playlist_into_memory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first = root / "first.ogg"
            second = root / "second.flac"
            skipped = root / "ignored.wav"
            first.write_bytes(b"first-track-data")
            second.write_bytes(b"second-track-data")
            skipped.write_bytes(b"not-a-drm-playlist-file")
            played = []

            server = LocalDRMPlaylistServer(
                [str(first), str(second), str(skipped)],
                loop=False,
                on_track=lambda *details: played.append(details),
            )
            try:
                playlist = urlopen(server.url, timeout=3).read().decode("utf-8")
                self.assertTrue(playlist.startswith("#EXTM3U\n"))
                self.assertEqual(urlopen(playlist.splitlines()[1], timeout=3).read(), b"first-track-data")

                playlist = urlopen(server.url, timeout=3).read().decode("utf-8")
                self.assertEqual(urlopen(playlist.splitlines()[1], timeout=3).read(), b"second-track-data")

                with self.assertRaises(HTTPError) as error:
                    urlopen(server.url, timeout=3)
                self.assertEqual(error.exception.code, 410)
                self.assertTrue(server.finished.is_set())
                self.assertEqual([entry[1] for entry in played], [1, 2])
                self.assertEqual([entry[2] for entry in played], [2, 2])
            finally:
                server.close()

    def test_rejects_playlist_with_no_supported_files(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "audio.wav"
            path.write_bytes(b"wav")
            with self.assertRaisesRegex(ValueError, "OGG, MP3, or FLAC"):
                LocalDRMPlaylistServer([str(path)])


if __name__ == "__main__":
    unittest.main()
