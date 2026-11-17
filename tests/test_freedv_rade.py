import io
import unittest
from unittest import mock

import numpy as np

from freedv_rade import (
    RADE_AUDIO_CENTER_HZ,
    RADE_CHANNEL_BANDWIDTH_HZ,
    RADE_INPUT_FRAME_SAMPLES,
    RADE_MODEM_SAMPLE_RATE,
    RADE_PREROLL_FRAMES,
    RADE_SPEECH_SAMPLE_RATE,
    StreamingDecimator3,
    StreamingLinearResampler,
    _read_exact,
    prepare_rade_encoder,
)


class FreeDVRadePipelineTests(unittest.TestCase):
    def test_profile_matches_rade_v1_speech_and_modem_rates(self):
        self.assertEqual(RADE_SPEECH_SAMPLE_RATE, 16_000)
        self.assertEqual(RADE_MODEM_SAMPLE_RATE, 8_000)
        self.assertEqual(RADE_INPUT_FRAME_SAMPLES, 160)
        self.assertEqual(RADE_AUDIO_CENTER_HZ, 1_500)
        self.assertEqual(RADE_CHANNEL_BANDWIDTH_HZ, 1_500)
        self.assertGreater(RADE_PREROLL_FRAMES, 0)

    def test_decimator_converts_48khz_to_16khz(self):
        source = np.zeros(48_000, dtype=np.float32)
        result = StreamingDecimator3().process(source)
        self.assertEqual(result.size, 16_000)
        self.assertEqual(result.dtype, np.float32)

    def test_decimator_is_independent_of_callback_block_boundaries(self):
        samples = np.random.default_rng(7).normal(0.0, 0.2, 48_000).astype(np.float32)
        expected = StreamingDecimator3().process(samples)

        decimator = StreamingDecimator3()
        blocks = [
            decimator.process(samples[start:start + 512])
            for start in range(0, samples.size, 512)
        ]
        actual = np.concatenate(blocks)

        np.testing.assert_allclose(actual, expected, atol=2e-7)
        self.assertEqual(actual.size, 16_000)

    def test_decimator_rejects_out_of_band_audio_before_downsampling(self):
        index = np.arange(48_000, dtype=np.float64)
        source = (
            0.5 * np.sin(2.0 * np.pi * 10_000 * index / 48_000)
        ).astype(np.float32)
        result = StreamingDecimator3().process(source)[1_000:]
        self.assertLess(float(np.sqrt(np.mean(result.astype(np.float64) ** 2))), 0.01)

    def test_resampler_is_independent_of_modem_block_boundaries(self):
        index = np.arange(8_000, dtype=np.float64)
        source = np.sin(2.0 * np.pi * 1_500 * index / 8_000).astype(np.float32)
        expected = StreamingLinearResampler(8_000, 48_000).process(source)

        resampler = StreamingLinearResampler(8_000, 48_000)
        blocks = [
            resampler.process(source[start:start + 960])
            for start in range(0, source.size, 960)
        ]
        actual = np.concatenate(blocks)
        np.testing.assert_allclose(actual, expected, atol=2e-6)

    def test_read_exact_handles_short_reads(self):
        class ShortReader(io.BytesIO):
            def read(self, size=-1):
                return super().read(min(size, 3))

        self.assertEqual(_read_exact(ShortReader(b"abcdefghij"), 7), b"abcdefg")

    def test_prepare_runtime_explains_unsupported_platform(self):
        with mock.patch("freedv_rade.os.name", "posix"):
            with self.assertRaisesRegex(RuntimeError, "currently available for Windows x64"):
                prepare_rade_encoder()


if __name__ == "__main__":
    unittest.main()
