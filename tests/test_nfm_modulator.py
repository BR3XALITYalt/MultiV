import unittest

import numpy as np

from nfm_modulator import (
    NFM_IF_HZ,
    NFM_DEVIATION_HZ,
    NFM_AUDIO_HIGH_HZ,
    NFM_AUDIO_LOW_HZ,
    NFM_CHANNEL_BANDWIDTH_HZ,
    NFM_OUTPUT_LEVEL,
    NFM_PROFILES,
    SAMPLE_RATE_HZ,
    StreamingNFMModulator,
    nfm_profile_settings,
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

        lower_sideband = float(spectrum[4_000])
        upper_sideband = float(spectrum[6_000])
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

    def test_corrected_nfm_profile_keeps_spectral_guard_at_dc(self):
        self.assertEqual(NFM_IF_HZ, 5_000)
        self.assertEqual(NFM_DEVIATION_HZ, 2_500)
        self.assertEqual(NFM_AUDIO_LOW_HZ, 30)
        self.assertEqual(NFM_AUDIO_HIGH_HZ, 2_000)
        self.assertEqual(NFM_CHANNEL_BANDWIDTH_HZ, 9_000)
        self.assertGreater(
            NFM_IF_HZ - NFM_DEVIATION_HZ - NFM_AUDIO_HIGH_HZ,
            0,
        )

    def test_wide_profile_expands_voice_band_without_clipping_the_if(self):
        settings = nfm_profile_settings("Wide (11 kHz)")
        self.assertEqual(settings["if_hz"], 6_000)
        self.assertEqual(settings["deviation_hz"], 2_500)
        self.assertEqual(settings["audio_low_hz"], 100)
        self.assertEqual(settings["audio_high_hz"], 3_000)
        self.assertEqual(settings["channel_bandwidth_hz"], 11_000)
        self.assertEqual(settings["transport_bandwidth_hz"], 12_000)
        self.assertGreater(
            settings["if_hz"] - settings["deviation_hz"] - settings["audio_high_hz"],
            0,
        )

        modulator = StreamingNFMModulator(
            if_hz=settings["if_hz"],
            deviation_hz=settings["deviation_hz"],
            low_cut_hz=settings["audio_low_hz"],
            high_cut_hz=settings["audio_high_hz"],
        )
        output = modulator.process(np.zeros(512, dtype=np.float32))
        self.assertEqual(output.dtype, np.float32)
        self.assertTrue(np.isfinite(output).all())

    def test_nfm_profiles_keep_the_existing_standard_default(self):
        self.assertEqual(NFM_PROFILES[0], "Standard (9 kHz)")
        self.assertEqual(nfm_profile_settings(NFM_PROFILES[0])["if_hz"], NFM_IF_HZ)
        with self.assertRaises(ValueError):
            nfm_profile_settings("Unknown profile")

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
