"""Streaming narrowband FM modulation for MultiV."""

from __future__ import annotations

import math

import numpy as np


SAMPLE_RATE_HZ = 48_000
NFM_IF_HZ = 5_000
NFM_DEVIATION_HZ = 2_500
NFM_AUDIO_LOW_HZ = 30
NFM_AUDIO_HIGH_HZ = 2_000
NFM_CHANNEL_BANDWIDTH_HZ = 9_000
NFM_OUTPUT_LEVEL = 0.70
NFM_BASE_INPUT_GAIN = 1.0

NFM_PROFILES = ("Standard (9 kHz)", "Wide (11 kHz)")
NFM_DEFAULT_PROFILE = NFM_PROFILES[0]
_NFM_PROFILE_SETTINGS = {
    NFM_PROFILES[0]: {
        "if_hz": 5_000,
        "deviation_hz": 2_500,
        "audio_low_hz": 30,
        "audio_high_hz": 2_000,
        "channel_bandwidth_hz": 9_000,
        "transport_bandwidth_hz": 12_000,
    },
    NFM_PROFILES[1]: {
        "if_hz": 6_000,
        "deviation_hz": 2_500,
        "audio_low_hz": 100,
        "audio_high_hz": 3_000,
        "channel_bandwidth_hz": 11_000,
        "transport_bandwidth_hz": 12_000,
    },
}


def nfm_profile_settings(profile: str) -> dict[str, int]:
    """Return a copy of the selected NFM profile's modulation and filter settings."""
    selected = str(profile or NFM_DEFAULT_PROFILE).strip()
    try:
        return dict(_NFM_PROFILE_SETTINGS[selected])
    except KeyError as exc:
        raise ValueError(
            f"Unknown NFM profile: {selected}. Choose one of: {', '.join(NFM_PROFILES)}."
        ) from exc


class _Biquad:
    """Second-order Butterworth high-pass or low-pass filter with streaming state."""

    def __init__(self, cutoff_hz: int, sample_rate_hz: int, *, high_pass: bool) -> None:
        if not 0 < cutoff_hz < sample_rate_hz / 2:
            raise ValueError("NFM filter cutoff must be below the Nyquist frequency.")

        omega = 2.0 * math.pi * cutoff_hz / sample_rate_hz
        cosine = math.cos(omega)
        sine = math.sin(omega)
        alpha = sine / (2.0 * math.sqrt(0.5))

        if high_pass:
            b0 = (1.0 + cosine) / 2.0
            b1 = -(1.0 + cosine)
            b2 = (1.0 + cosine) / 2.0
        else:
            b0 = (1.0 - cosine) / 2.0
            b1 = 1.0 - cosine
            b2 = (1.0 - cosine) / 2.0

        a0 = 1.0 + alpha
        a1 = -2.0 * cosine
        a2 = 1.0 - alpha

        self.b0 = b0 / a0
        self.b1 = b1 / a0
        self.b2 = b2 / a0
        self.a1 = a1 / a0
        self.a2 = a2 / a0
        self.z1 = 0.0
        self.z2 = 0.0

    def process(self, sample: float) -> float:
        """Filter one sample while preserving state across callback blocks."""
        output = self.b0 * sample + self.z1
        self.z1 = self.b1 * sample - self.a1 * output + self.z2
        self.z2 = self.b2 * sample - self.a2 * output
        return output


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
        self._high_pass = _Biquad(
            self.low_cut_hz,
            self.sample_rate_hz,
            high_pass=True,
        )
        self._low_pass = _Biquad(
            self.high_cut_hz,
            self.sample_rate_hz,
            high_pass=False,
        )
        self._input_gain = NFM_BASE_INPUT_GAIN * (
            10.0 ** (float(input_gain_db) / 20.0)
        )
        self._phase = 0.0

    def process(self, samples) -> np.ndarray:
        """Filter and frequency-modulate one mono Float32 audio block."""
        source = np.asarray(samples, dtype=np.float64).reshape(-1)
        if source.size == 0:
            return np.empty(0, dtype=np.float32)

        source = np.nan_to_num(source, nan=0.0, posinf=0.0, neginf=0.0)
        voice = np.empty(source.size, dtype=np.float64)
        for index, sample in enumerate(source):
            high_passed = self._high_pass.process(float(sample))
            voice[index] = self._low_pass.process(high_passed) * self._input_gain

        voice = soft_clip(voice)

        phase_steps = (
            2.0 * math.pi
            * (self.if_hz + self.deviation_hz * voice)
            / self.sample_rate_hz
        )
        phase = self._phase + np.cumsum(phase_steps, dtype=np.float64)
        self._phase = float(np.remainder(phase[-1], 2.0 * math.pi))

        return (self.output_level * np.cos(phase)).astype(np.float32)
