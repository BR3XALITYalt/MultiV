import tempfile
import unittest
from pathlib import Path

from playlist_decoder import iter_playlist_audio_blocks


class PlaylistDecoderTests(unittest.TestCase):
    @staticmethod
    def _write_audio(path: Path, muxer: str, codec: str) -> None:
        import av
        import numpy as np

        output = av.open(str(path), mode="w", format=muxer)
        try:
            stream = output.add_stream(codec, rate=48_000)
            stream.layout = "stereo"
            timeline = np.arange(30_720, dtype=np.float32) / 48_000
            signal = (0.2 * np.sin(2 * np.pi * 440 * timeline)).astype(np.float32)
            samples = np.vstack((signal, signal))
            frame_size = 1_024 if codec == "aac" else 960

            for offset in range(0, samples.shape[1], frame_size):
                frame = av.AudioFrame.from_ndarray(
                    samples[:, offset:offset + frame_size],
                    format="fltp",
                    layout="stereo",
                )
                frame.sample_rate = 48_000
                for packet in stream.encode(frame):
                    output.mux(packet)

            for packet in stream.encode(None):
                output.mux(packet)
        finally:
            output.close()

    def _assert_decodes(self, path: Path) -> None:
        blocks = list(iter_playlist_audio_blocks(path, block_frames=1_024))
        self.assertTrue(blocks)
        self.assertTrue(all(rate == 48_000 for rate, _block in blocks))
        self.assertTrue(all(block.ndim == 2 and block.shape[1] == 1 for _rate, block in blocks))
        self.assertGreater(sum(block.shape[0] for _rate, block in blocks), 0)

    def test_decodes_m4a_audio(self):
        try:
            import av  # noqa: F401
        except ImportError:
            self.skipTest("PyAV is not installed.")

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sample.m4a"
            try:
                self._write_audio(path, "ipod", "aac")
            except Exception as exc:
                self.skipTest(f"The bundled FFmpeg AAC encoder is unavailable: {exc}")
            self._assert_decodes(path)

    def test_decodes_ogg_opus_audio(self):
        try:
            import av  # noqa: F401
        except ImportError:
            self.skipTest("PyAV is not installed.")

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sample.opus"
            try:
                self._write_audio(path, "ogg", "libopus")
            except Exception as exc:
                self.skipTest(f"The bundled FFmpeg Opus encoder is unavailable: {exc}")
            self._assert_decodes(path)


if __name__ == "__main__":
    unittest.main()
