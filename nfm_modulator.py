"""Streaming narrowband FM modulation for TXITPY."""

from __future__ import annotations

import math

import numpy as np


SAMPLE_RATE_HZ = 48_000
NFM_IF_HZ = 6_000
NFM_DEVIATION_HZ = 2_500
NFM_AUDIO_LOW_HZ = 30
NFM_AUDIO_HIGH_HZ = 3_000
NFM_CHANNEL_BANDWIDTH_HZ = 11_000
NFM_OUTPUT_LEVEL = 0.70
NFM_BASE_INPUT_GAIN = 1.0


def soft_clip(samples: np.ndarray, knee: float = 0.8) -> np.ndarray:
    """Soft-limit peaks while keeping signals below the knee unchanged."""
    values = np.asarray(samples, dtype=np.float64)
    limit_range = 1.0 - knee
    magnitude = np.abs(values)
    softened = knee + limit_range * np.tanh(
        np.maximum(magnitude - knee, 0.0) / limit_range
    )
    return np.sign(values) * np.where(magnitude <= knee, magnitude, softened)


class StreamingNFMModulator:
    """Convert mono voice audio into a continuous real NFM intermediate-frequency signal."""

    def __init__(
        self,
        *,
        sample_rate_hz: int = SAMPLE_RATE_HZ,
        if_hz: int = NFM_IF_HZ,
        deviation_hz: int = NFM_DEVIATION_HZ,
        low_cut_hz: int = NFM_AUDIO_LOW_HZ,
        high_cut_hz: int = NFM_AUDIO_HIGH_HZ,
        input_gain_db: float = 0.0,
        output_level: float = NFM_OUTPUT_LEVEL,
    ) -> None:
        if sample_rate_hz <= 0:
            raise ValueError("NFM sample rate must be positive.")
        if not 0 < low_cut_hz < high_cut_hz:
            raise ValueError("NFM audio filter limits are invalid.")
        if if_hz <= deviation_hz + high_cut_hz:
            raise ValueError("NFM intermediate frequency is too low for its deviation and audio bandwidth.")
        if if_hz + deviation_hz + high_cut_hz >= sample_rate_hz / 2:
            raise ValueError("NFM signal exceeds the Nyquist frequency.")
        if not -6.0 <= float(input_gain_db) <= 6.0:
            raise ValueError("NFM input gain must be between -6 and +6 dB.")
        if not 0.0 < float(output_level) <= 1.0:
            raise ValueError("NFM output level must be between 0 and 1.")

        self.sample_rate_hz = int(sample_rate_hz)
        self.if_hz = int(if_hz)
        self.deviation_hz = int(deviation_hz)
        self.low_cut_hz = int(low_cut_hz)
        self.high_cut_hz = int(high_cut_hz)
        self.output_level = float(output_level)

        self._high_pass_alpha = math.exp(
            -2.0 * math.pi * self.low_cut_hz / self.sample_rate_hz
        )
        self._low_pass_alpha = 1.0 - math.exp(
            -2.0 * math.pi * self.high_cut_hz / self.sample_rate_hz
        )
        self._input_gain = NFM_BASE_INPUT_GAIN * (
            10.0 ** (float(input_gain_db) / 20.0)
        )

        self._previous_input = 0.0
        self._previous_high_pass = 0.0
        self._previous_low_pass = 0.0
        self._phase = 0.0

    def process(self, samples) -> np.ndarray:
        """Filter and frequency-modulate one mono Float32 audio block."""
        source = np.asarray(samples, dtype=np.float64).reshape(-1)
        if source.size == 0:
            return np.empty(0, dtype=np.float32)

        source = np.nan_to_num(source, nan=0.0, posinf=0.0, neginf=0.0)
        voice = np.empty(source.size, dtype=np.float64)

        previous_input = self._previous_input
        previous_high_pass = self._previous_high_pass
        previous_low_pass = self._previous_low_pass

        for index, sample in enumerate(source):
            high_pass = self._high_pass_alpha * (
                previous_high_pass + float(sample) - previous_input
            )
            previous_input = float(sample)
            previous_high_pass = high_pass
            previous_low_pass += self._low_pass_alpha * (
                high_pass - previous_low_pass
            )
            voice[index] = previous_low_pass * self._input_gain

        self._previous_input = previous_input
        self._previous_high_pass = previous_high_pass
        self._previous_low_pass = previous_low_pass

        voice = soft_clip(voice)

        phase_steps = (
            2.0 * math.pi
            * (self.if_hz + self.deviation_hz * voice)
            / self.sample_rate_hz
        )
        phase = self._phase + np.cumsum(phase_steps, dtype=np.float64)
        self._phase = float(np.remainder(phase[-1], 2.0 * math.pi))

        return (self.output_level * np.cos(phase)).astype(np.float32)
