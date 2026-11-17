import unittest

import numpy as np

from nfm_modulator import (
    NFM_IF_HZ,
    NFM_OUTPUT_LEVEL,
    SAMPLE_RATE_HZ,
    StreamingNFMModulator,
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

    def test_output_is_finite_float32_and_level_limited(self):
        rng = np.random.default_rng(42)
        source = rng.normal(0.0, 1.5, 8_192).astype(np.float32)
        output = StreamingNFMModulator().process(source)

        self.assertEqual(output.dtype, np.float32)
        self.assertTrue(np.isfinite(output).all())
        self.assertLessEqual(float(np.max(np.abs(output))), NFM_OUTPUT_LEVEL + 1e-6)

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
