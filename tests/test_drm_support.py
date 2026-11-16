import ast
import os
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
    _visual_studio_build_tools_command,
    _visual_studio_cmake_generator_for_version,
    _clear_stale_opus_cmake_builds,
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

    def test_run_command_passes_the_requested_build_environment(self):
        environment = os.environ.copy()
        environment["TXITPY_TEST_CMAKE_GENERATOR"] = "Visual Studio 17 2022"
        output = _run_command(
            [
                sys.executable,
                "-c",
                "import os; print(os.environ['TXITPY_TEST_CMAKE_GENERATOR'])",
            ],
            env=environment,
        )
        self.assertEqual(output, "Visual Studio 17 2022")

    def test_cmake_generator_matches_the_installed_visual_studio_version(self):
        self.assertEqual(
            _visual_studio_cmake_generator_for_version("17.11.5.0"),
            "Visual Studio 17 2022",
        )
        self.assertEqual(
            _visual_studio_cmake_generator_for_version("18.0.0.0"),
            "Visual Studio 18 2026",
        )
        self.assertEqual(
            _visual_studio_cmake_generator_for_version("16.11.47.0"),
            "Visual Studio 16 2019",
        )
        self.assertIsNone(_visual_studio_cmake_generator_for_version("19.0.0.0"))
        self.assertIsNone(_visual_studio_cmake_generator_for_version("unknown"))

    def test_stale_opus_cmake_cache_is_removed_when_generator_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            build_root = Path(folder) / "target" / "release" / "build"
            build_directory = (
                build_root
                / "decdrm-opus-sys-example"
                / "out"
                / "build"
            )
            build_directory.mkdir(parents=True)
            cache = build_directory / "CMakeCache.txt"
            cache.write_text(
                "CMAKE_GENERATOR:INTERNAL=Visual Studio 18 2026\n",
                encoding="utf-8",
            )
            retained_file = build_directory.parent / "important.txt"
            retained_file.write_text("keep", encoding="utf-8")

            removed = _clear_stale_opus_cmake_builds(
                build_root,
                "Visual Studio 17 2022",
            )

            self.assertEqual(removed, 1)
            self.assertFalse(build_directory.exists())
            self.assertEqual(retained_file.read_text(encoding="utf-8"), "keep")

    def test_matching_opus_cmake_cache_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            build_root = Path(folder) / "target" / "release" / "build"
            build_directory = (
                build_root
                / "decdrm-opus-sys-example"
                / "out"
                / "build"
            )
            build_directory.mkdir(parents=True)
            cache = build_directory / "CMakeCache.txt"
            cache.write_text(
                "CMAKE_GENERATOR:INTERNAL=Visual Studio 17 2022\n",
                encoding="utf-8",
            )

            removed = _clear_stale_opus_cmake_builds(
                build_root,
                "Visual Studio 17 2022",
            )

            self.assertEqual(removed, 0)
            self.assertTrue(cache.is_file())

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

    def test_build_tools_installer_only_modifies_buildtools_installations(self):
        installer = Path("vs_BuildTools.exe")
        buildtools = Path("C:/Program Files/Microsoft Visual Studio/18/BuildTools")
        community = Path("C:/Program Files/Microsoft Visual Studio/18/Community")

        buildtools_command = _visual_studio_build_tools_command(installer, buildtools)
        community_command = _visual_studio_build_tools_command(installer, community)

        self.assertIn("modify", buildtools_command)
        self.assertIn(str(buildtools), buildtools_command)
        self.assertNotIn("modify", community_command)
        self.assertIn(VISUAL_STUDIO_WORKLOAD, community_command)
        self.assertIn("--includeRecommended", community_command)

    def test_drm_setup_callback_starts_the_worker_without_recursing(self):
        main_path = Path(__file__).resolve().parents[1] / "main.py"
        syntax_tree = ast.parse(main_path.read_text(encoding="utf-8"))
        setup_function = next(
            node for node in ast.walk(syntax_tree)
            if isinstance(node, ast.FunctionDef) and node.name == "start_drm_setup"
        )
        called_names = {
            node.func.id
            for node in ast.walk(setup_function)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }

        self.assertNotIn("start_drm_setup", called_names)
        self.assertIn("prepare_drm_encoder", called_names)
        self.assertIn("setup_worker", {
            node.name for node in ast.walk(setup_function)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        })

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
        self.assertEqual(config["channel"]["msc_mode"], "64-QAM")
        self.assertEqual(config["channel"]["protection_b"], 1)
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
