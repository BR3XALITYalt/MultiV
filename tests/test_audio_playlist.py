import tempfile
import unittest
from pathlib import Path

from audio_playlist import (
    SUPPORTED_AUDIO_EXTENSIONS,
    build_playlist_order,
    build_playlist_snapshot,
    collect_audio_files,
    normalize_audio_sources,
)


class ReverseRandom:
    def shuffle(self, values):
        values.reverse()


class AudioPlaylistTests(unittest.TestCase):
    def test_supported_extensions_include_common_audio_formats(self):
        self.assertTrue({
            ".wav", ".flac", ".ogg", ".aiff", ".aif", ".mp3", ".m4a", ".opus"
        }.issubset(SUPPORTED_AUDIO_EXTENSIONS))

    def test_collects_audio_files_recursively_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nested = root / "nested"
            nested.mkdir()
            files = (
                root / "z-track.MP3",
                root / "a-track.wav",
                nested / "b-track.flac",
                nested / "ignore.txt",
            )
            for file_path in files:
                file_path.write_bytes(b"test")

            collected = collect_audio_files([root, files[1], nested])
            self.assertEqual(
                [Path(item).name for item in collected],
                ["a-track.wav", "b-track.flac", "z-track.MP3"],
            )

    def test_normalizes_file_and_folder_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            track = root / "track.ogg"
            folder = root / "music"
            folder.mkdir()
            track.write_bytes(b"audio")

            sources = normalize_audio_sources([track, folder, track, root / "missing"])
            self.assertEqual(
                sources,
                [str(track.resolve()), str(folder.resolve()), str((root / "missing").resolve())],
            )

    def test_playlist_snapshot_detects_added_and_modified_tracks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.ogg"
            first.write_bytes(b"first")

            initial_files, initial_signature = build_playlist_snapshot([root])
            self.assertEqual([Path(path).name for path in initial_files], ["first.ogg"])

            second = root / "second.ogg"
            second.write_bytes(b"second")
            added_files, added_signature = build_playlist_snapshot([root])
            self.assertEqual(
                [Path(path).name for path in added_files],
                ["first.ogg", "second.ogg"],
            )
            self.assertNotEqual(initial_signature, added_signature)

            first.write_bytes(b"first track changed")
            modified_files, modified_signature = build_playlist_snapshot([root])
            self.assertEqual(modified_files, added_files)
            self.assertNotEqual(added_signature, modified_signature)

    def test_playlist_snapshot_honors_removed_tracks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.wav"
            second = root / "second.wav"
            first.write_bytes(b"first")
            second.write_bytes(b"second")

            files, _signature = build_playlist_snapshot([root], [first])
            self.assertEqual([Path(path).name for path in files], ["second.wav"])

    def test_reimporting_a_folder_restores_previously_excluded_tracks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.ogg"
            second = root / "second.ogg"
            first.write_bytes(b"first")
            second.write_bytes(b"second")

            removed, _signature = build_playlist_snapshot([root], [first])
            restored, _restored_signature = build_playlist_snapshot([root], [])
            self.assertEqual([Path(path).name for path in removed], ["second.ogg"])
            self.assertEqual(
                [Path(path).name for path in restored],
                ["first.ogg", "second.ogg"],
            )

    def test_ignores_missing_paths_and_unsupported_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unsupported = root / "notes.txt"
            unsupported.write_text("not audio", encoding="utf-8")
            self.assertEqual(
                collect_audio_files([root / "missing.wav", unsupported]),
                [],
            )

    def test_playlist_order_preserves_input_when_shuffle_is_disabled(self):
        files = ["one.wav", "two.wav", "three.wav"]
        self.assertEqual(build_playlist_order(files), files)

    def test_shuffle_avoids_repeating_the_last_track_at_loop_boundary(self):
        order = build_playlist_order(
            ["one.wav", "two.wav", "three.wav"],
            shuffle=True,
            previous_file="three.wav",
            rng=ReverseRandom(),
        )
        self.assertEqual(order, ["two.wav", "three.wav", "one.wav"])
        self.assertNotEqual(order[0], "three.wav")

    def test_ogg_vorbis_files_can_be_decoded(self):
        import numpy as np
        import soundfile

        self.assertIn("OGG", soundfile.available_formats())
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "test.ogg"
            source = np.linspace(-0.5, 0.5, 4_800, dtype=np.float32)
            soundfile.write(
                str(path),
                source,
                48_000,
                format="OGG",
                subtype="VORBIS",
            )

            with soundfile.SoundFile(str(path)) as audio:
                decoded = audio.read(dtype="float32", always_2d=True)

            self.assertGreater(decoded.size, 0)
            self.assertEqual(decoded.shape[1], 1)
            self.assertEqual(audio.samplerate, 48_000)

    def test_single_track_playlist_remains_valid_when_shuffled(self):
        self.assertEqual(
            build_playlist_order(
                ["only.wav"],
                shuffle=True,
                previous_file="only.wav",
                rng=ReverseRandom(),
            ),
            ["only.wav"],
        )


if __name__ == "__main__":
    unittest.main()
