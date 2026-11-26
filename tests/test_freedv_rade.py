import io
import queue
import threading
import unittest
from unittest import mock

import numpy as np

from freedv_rade import (
    RADE_AUDIO_CENTER_HZ,
    RADE_CHANNEL_BANDWIDTH_HZ,
    RADE_INPUT_FRAME_SAMPLES,
    RADE_MODEM_SAMPLE_RATE,
    FreeDVRadeEncoder,
    RADE_PREROLL_FRAMES,
    RADE_SPEECH_SAMPLE_RATE,
    StreamingDecimator3,
    StreamingLinearResampler,
    _read_exact,
    _runtime_spec,
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

    def test_feed_releases_input_lock_when_stop_cancels_queue_wait(self):
        encoder = object.__new__(FreeDVRadeEncoder)
        encoder._input_lock = threading.Lock()
        encoder._error_lock = threading.Lock()
        encoder._error_text = ""
        encoder._stderr_lines = []
        encoder._accepting = True
        encoder._process = mock.Mock()
        encoder._process.poll.return_value = None
        encoder._decimator = mock.Mock()
        encoder._decimator.process.return_value = np.ones(
            RADE_INPUT_FRAME_SAMPLES,
            dtype=np.float32,
        )
        encoder._speech_buffer = np.empty(0, dtype=np.float32)
        entered_put = threading.Event()

        class SignallingQueue(queue.Queue):
            def put(self, item, block=True, timeout=None):
                if timeout is not None:
                    entered_put.set()
                return super().put(item, block=block, timeout=timeout)

        encoder._input_queue = SignallingQueue(maxsize=1)
        encoder._input_queue.put_nowait(b"already queued")
        cancel_event = threading.Event()
        errors = []

        def feed():
            try:
                encoder.feed(
                    np.ones(480, dtype=np.float32),
                    wait_for_space=True,
                    cancel_event=cancel_event,
                )
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=feed, daemon=True)
        worker.start()
        self.assertTrue(entered_put.wait(1))
        cancel_event.set()
        worker.join(timeout=1)

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertRegex(str(errors[0]), "stopped while queuing audio")
        self.assertTrue(encoder._input_lock.acquire(blocking=False))
        encoder._input_lock.release()

    def test_read_exact_handles_short_reads(self):
        class ShortReader(io.BytesIO):
            def read(self, size=-1):
                return super().read(min(size, 3))

        self.assertEqual(_read_exact(ShortReader(b"abcdefghij"), 7), b"abcdefg")

    def test_windows_runtime_uses_the_verified_windows_package(self):
        with mock.patch("freedv_rade.os.name", "nt"):
            asset, executable, libraries = _runtime_spec()

        self.assertEqual(asset, "txitpy-rade-v1-windows-x64.zip")
        self.assertEqual(executable, "freedv_rade_tx.exe")
        self.assertIn("rade.dll", libraries)

    def test_linux_x64_runtime_uses_the_native_linux_package(self):
        with (
            mock.patch("freedv_rade.os.name", "posix"),
            mock.patch("freedv_rade.platform.system", return_value="Linux"),
            mock.patch("freedv_rade.platform.machine", return_value="x86_64"),
        ):
            asset, executable, libraries = _runtime_spec()

        self.assertEqual(asset, "txitpy-rade-v1-linux-x86_64.zip")
        self.assertEqual(executable, "freedv_rade_tx")
        self.assertEqual(libraries, ("librade.so*",))

    def test_linux_arm_runtime_reports_that_no_prebuilt_package_exists(self):
        with (
            mock.patch("freedv_rade.os.name", "posix"),
            mock.patch("freedv_rade.platform.system", return_value="Linux"),
            mock.patch("freedv_rade.platform.machine", return_value="aarch64"),
        ):
            with self.assertRaisesRegex(RuntimeError, "Linux x86_64"):
                _runtime_spec()


if __name__ == "__main__":
    unittest.main()
