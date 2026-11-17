import unittest

import numpy as np

from nfm_modulator import (
    NFM_IF_HZ,
    NFM_DEVIATION_HZ,
    NFM_AUDIO_HIGH_HZ,
    NFM_AUDIO_LOW_HZ,
    NFM_CHANNEL_BANDWIDTH_HZ,
    NFM_OUTPUT_LEVEL,
    SAMPLE_RATE_HZ,
    StreamingNFMModulator,
    soft_clip,
)


class StreamingNFMModulatorTests(unittest.TestCase):
    def test_silence_produces_an_unmodulated_if_carrier(self):
        modulator = StreamingNFMModulator()
        output = modulator.process(np.zeros(512, dtype=np.float32))

        steps = 2.0 * np.pi * NFM_IF_HZ / SAMPLE_RATE_HZ
        expected = NFM_OUTPUT_LEVEL * np.cos(
            np.arange(1, 513, dtype=np.float64) * steps
        )

        self.assertEqual(output.dtype, np.float32)
        np.testing.assert_allclose(output, expected, atol=2e-6)

    def test_processing_is_independent_of_audio_block_boundaries(self):
        source = (
            0.2 * np.sin(
                2.0 * np.pi * 1_000
                * np.arange(12_000, dtype=np.float64)
                / SAMPLE_RATE_HZ
            )
        ).astype(np.float32)

        one_block = StreamingNFMModulator().process(source)
        chunked_modulator = StreamingNFMModulator()
        chunks = [
            chunked_modulator.process(source[start:start + 512])
            for start in range(0, source.size, 512)
        ]
        chunked = np.concatenate(chunks)

        np.testing.assert_allclose(chunked, one_block, atol=2e-6)

    def test_single_tone_fm_sidebands_are_symmetric_before_transport(self):
        sample_count = 2 * SAMPLE_RATE_HZ
        sample_index = np.arange(sample_count, dtype=np.float64)
        source = (
            0.25
            * np.sin(2.0 * np.pi * 1_000 * sample_index / SAMPLE_RATE_HZ)
        ).astype(np.float32)

        modulator = StreamingNFMModulator()
        chunks = [
            modulator.process(source[start:start + 512])
            for start in range(0, source.size, 512)
        ]
        output = np.concatenate(chunks)[SAMPLE_RATE_HZ:]
        spectrum = np.abs(np.fft.rfft(output))

        lower_sideband = float(spectrum[5_000])
        upper_sideband = float(spectrum[7_000])
        self.assertGreater(lower_sideband, 0.01)
        self.assertAlmostEqual(
            lower_sideband,
            upper_sideband,
            delta=lower_sideband * 1e-5,
        )

    def test_output_is_finite_float32_and_level_limited(self):
        rng = np.random.default_rng(42)
        source = rng.normal(0.0, 1.5, 8_192).astype(np.float32)
        output = StreamingNFMModulator().process(source)

        self.assertEqual(output.dtype, np.float32)
        self.assertTrue(np.isfinite(output).all())
        self.assertLessEqual(float(np.max(np.abs(output))), NFM_OUTPUT_LEVEL + 1e-6)

    def test_audio_high_pass_cutoff_preserves_deep_bass(self):
        self.assertEqual(NFM_AUDIO_LOW_HZ, 30)

    def test_wider_nfm_profile_fits_the_configured_ssb_transport(self):
        self.assertEqual(NFM_IF_HZ, 6_000)
        self.assertEqual(NFM_DEVIATION_HZ, 2_500)
        self.assertEqual(NFM_AUDIO_LOW_HZ, 30)
        self.assertEqual(NFM_AUDIO_HIGH_HZ, 3_000)
        self.assertEqual(NFM_CHANNEL_BANDWIDTH_HZ, 11_000)
        self.assertLess(
            NFM_IF_HZ + NFM_DEVIATION_HZ + NFM_AUDIO_HIGH_HZ,
            12_000,
        )

    def test_soft_limiter_preserves_quiet_audio_and_rounds_peaks(self):
        source = np.array([-2.0, -0.4, 0.0, 0.4, 2.0], dtype=np.float64)
        output = soft_clip(source)

        np.testing.assert_array_equal(output[1:4], source[1:4])
        self.assertLess(output[0], -0.8)
        self.assertGreater(output[0], -1.0)
        self.assertGreater(output[-1], 0.8)
        self.assertLess(output[-1], 1.0)
        self.assertTrue(np.all(np.diff(output) > 0))

    def test_empty_blocks_do_not_change_modulator_state(self):
        source = np.ones(512, dtype=np.float32) * 0.1
        first = StreamingNFMModulator()
        second = StreamingNFMModulator()

        expected = first.process(source)
        second.process(np.empty(0, dtype=np.float32))
        actual = second.process(source)

        np.testing.assert_array_equal(actual, expected)


if __name__ == "__main__":
    unittest.main()
