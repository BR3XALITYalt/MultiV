"""TXITY virtual transmitter: authenticated control and mono PCM audio streaming."""

from __future__ import annotations

import math
import os
import platform
import queue
import random
import re
import subprocess
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from audio_playlist import build_playlist_order, collect_audio_files
from playlist_decoder import iter_playlist_audio_blocks
from drm_support import (
    DRM_DEFAULT_QUALITY_PROFILE,
    LocalDRMPlaylistServer,
    drm_quality_profile_settings,
    ensure_drm_encoder,
    write_drm_station_config,
)
from nfm_modulator import (
    NFM_AUDIO_HIGH_HZ,
    NFM_DEFAULT_PROFILE,
    NFM_PROFILES,
    NFM_AUDIO_LOW_HZ,
    NFM_CHANNEL_BANDWIDTH_HZ,
    NFM_DEVIATION_HZ,
    NFM_IF_HZ,
    StreamingNFMModulator,
    nfm_profile_settings,
)
from freedv_rade import (
    FreeDVRadeEncoder,
    RADE_AUDIO_CENTER_HZ,
    RADE_AUDIO_QUEUE_FRAMES,
    RADE_CHANNEL_BANDWIDTH_HZ,
    RADE_PREROLL_FRAMES,
    prepare_rade_encoder,
)

from icecast_output import (
    ICECAST_BITRATES,
    IcecastBroadcaster,
    normalize_icecast_destination,
)
from icecast_server import EmbeddedIcecastServer


BASE_URL = "https://txity.radiodiscussion.org"
AUDIO_URL = "wss://txity.radiodiscussion.org/ws/audio"
SAMPLE_RATE_HZ = 48_000
BATCH_SAMPLES = 512
FRAME_BYTES = BATCH_SAMPLES * 4
AUDIO_QUEUE_FRAMES = 32
PLAYLIST_AUDIO_QUEUE_FRAMES = 512
PLAYLIST_PREROLL_FRAMES = 256
DRM_PREROLL_FRAMES = 94
PLAYLIST_READ_FRAMES = 8_192
AUDIO_FRAME_INTERVAL_SECONDS = BATCH_SAMPLES / SAMPLE_RATE_HZ
LIVE_AUDIO_SILENCE_GRACE_SECONDS = 0.1
RECONNECT_INITIAL_DELAY_SECONDS = 1.0
RECONNECT_MAX_DELAY_SECONDS = 15.0


def reconnect_delay(attempt: int) -> float:
    """Return a bounded exponential retry delay for a network reconnect."""
    exponent = max(0, min(int(attempt) - 1, 4))
    return min(
        RECONNECT_MAX_DELAY_SECONDS,
        RECONNECT_INITIAL_DELAY_SECONDS * (2 ** exponent),
    )


def playlist_frame_wait_seconds(next_frame_deadline: float | None, now: float) -> float:
    """Return the time remaining before the next playlist packet should be sent."""
    if next_frame_deadline is None:
        return 0.0
    return max(0.0, next_frame_deadline - now)


def next_audio_frame_deadline(
    current_deadline: float | None,
    now: float,
    *,
    catch_up_after_delay: bool = False,
) -> float:
    """Advance the audio clock without accumulating scheduler drift."""
    if current_deadline is None:
        return now + AUDIO_FRAME_INTERVAL_SECONDS

    next_deadline = current_deadline + AUDIO_FRAME_INTERVAL_SECONDS
    if now >= next_deadline:
        # Playlist playback skips a missed slot. DRM sends the next queued block
        # immediately, then resumes the sample clock.
        return now if catch_up_after_delay else now + AUDIO_FRAME_INTERVAL_SECONDS
    return next_deadline


def unpack_queued_audio_frame(item: Any) -> tuple[bytes, tuple[str, int, int] | None]:
    """Separate PCM bytes from optional playlist metadata before WebSocket sending."""
    if (
        isinstance(item, tuple)
        and len(item) == 2
        and isinstance(item[0], bytes)
    ):
        return item[0], item[1]
    if isinstance(item, bytes):
        return item, None
    raise TypeError(
        f"Audio queue item must contain PCM bytes, received {type(item).__name__}."
    )


def begin_windows_audio_timer_resolution():
    """Request 1 ms timer granularity while playlist audio is being streamed."""
    if os.name != "nt":
        return None
    try:
        import ctypes

        winmm = ctypes.WinDLL("winmm")
        winmm.timeBeginPeriod.argtypes = [ctypes.c_uint]
        winmm.timeBeginPeriod.restype = ctypes.c_uint
        winmm.timeEndPeriod.argtypes = [ctypes.c_uint]
        winmm.timeEndPeriod.restype = ctypes.c_uint
        if winmm.timeBeginPeriod(1) == 0:
            return winmm
    except (AttributeError, OSError):
        return None
    return None


def begin_windows_system_awake():
    """Prevent Windows from entering system sleep while this thread transmits."""
    if os.name != "nt":
        return None
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        set_execution_state = kernel32.SetThreadExecutionState
        set_execution_state.argtypes = [ctypes.c_uint]
        set_execution_state.restype = ctypes.c_uint
        previous_state = set_execution_state(0x80000001)
        if previous_state:
            return kernel32, set_execution_state
    except (AttributeError, OSError):
        return None
    return None


def end_windows_system_awake(handle) -> None:
    """Release the system-sleep request on the same thread that created it."""
    if handle is None:
        return
    _kernel32, set_execution_state = handle
    try:
        set_execution_state(0x80000000)
    except (AttributeError, OSError):
        pass


def should_send_live_audio_silence(last_audio_at: float, now: float) -> bool:
    """Fill a sustained microphone or loopback input gap with a silent audio frame."""
    return now - last_audio_at >= LIVE_AUDIO_SILENCE_GRACE_SECONDS


PTT_CONFIRMATION_GRACE_SECONDS = 4.0
SUPPORTED_MODES = ("AM", "USB", "LSB", "USB+C", "LSB+C", "DSB", "NFM", "RADE", "DRM", "CW")
DEFAULT_MODE_BANDWIDTH_HZ = {
    "AM": 12_000,
    "DSB": 12_000,
    "DRM": 12_000,
    "NFM": NFM_CHANNEL_BANDWIDTH_HZ,
    "RADE": RADE_CHANNEL_BANDWIDTH_HZ,
    "USB": 10_000,
    "LSB": 10_000,
    "USB+C": 10_000,
    "LSB+C": 10_000,
    "CW": 500,
}
SUPPORTED_AUDIO_SOURCES = ("mic", "loopback", "playlist")


def parse_rf_frequency(value: Any) -> int:
    """Parse TXITY-style frequency text into integer hertz."""
    text = str(value or "").strip().replace(",", "")
    match = re.fullmatch(
        r"([0-9]+(?:\.[0-9]+)?)\s*(hz|hertz|khz|kilohertz|mhz|megahertz)?",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        raise ValueError("Enter a frequency such as 7.250 MHz, 7250 kHz, or 7250000 Hz.")

    number = float(match.group(1))
    if not math.isfinite(number) or number <= 0:
        raise ValueError("Frequency must be a positive number.")

    unit = (match.group(2) or "").casefold()
    if unit in {"hz", "hertz"}:
        multiplier = 1
    elif unit in {"khz", "kilohertz"}:
        multiplier = 1_000
    elif unit in {"mhz", "megahertz"}:
        multiplier = 1_000_000
    elif number >= 1_000_000:
        multiplier = 1
    elif number >= 1_000:
        multiplier = 1_000
    else:
        multiplier = 1_000_000

    frequency = int(round(number * multiplier))
    if frequency <= 0:
        raise ValueError("Frequency must be greater than zero.")
    return frequency


TXITY_RF_ROOM_MIN_HZ = 7_000_000
TXITY_RF_ROOM_MAX_HZ = 8_000_000


def nfm_transport_settings(
    target_frequency_hz: Any,
    *,
    if_hz: int = NFM_IF_HZ,
) -> tuple[str, int]:
    """Place the selected NFM IF at the requested channel centre using TXITY SSB."""
    target_hz = parse_rf_frequency(target_frequency_hz)
    if not TXITY_RF_ROOM_MIN_HZ <= target_hz <= TXITY_RF_ROOM_MAX_HZ:
        raise ValueError("NFM runs only in TXITY's 7.000-8.000 MHz room.")
    intermediate_frequency = int(if_hz)
    if intermediate_frequency <= 0:
        raise ValueError("NFM intermediate frequency must be positive.")

    upper_carrier_hz = target_hz - intermediate_frequency
    if TXITY_RF_ROOM_MIN_HZ <= upper_carrier_hz <= TXITY_RF_ROOM_MAX_HZ:
        return "USB", upper_carrier_hz

    lower_carrier_hz = target_hz + intermediate_frequency
    if TXITY_RF_ROOM_MIN_HZ <= lower_carrier_hz <= TXITY_RF_ROOM_MAX_HZ:
        return "LSB", lower_carrier_hz

    raise ValueError("NFM needs enough tuning room for its selected intermediate frequency.")


def apply_nfm_transport(
    config: dict[str, Any],
    target_frequency_hz: Any,
) -> dict[str, Any]:
    """Apply a selected RF channel center to the underlying TXITY transport."""
    if str(config.get("_txitpyMode") or "").strip().upper() == "NFM":
        sideband, carrier_hz = nfm_transport_settings(
            target_frequency_hz,
            if_hz=int(config.get("_txitpyNfmIfHz", NFM_IF_HZ)),
        )
        config["mode"] = sideband
        config["rfHz"] = carrier_hz
        config["_txitpyNfmSideband"] = sideband
    else:
        config["rfHz"] = parse_rf_frequency(target_frequency_hz)
    return config


def rade_transport_settings(target_frequency_hz: Any) -> tuple[str, int]:
    """Use the requested frequency directly as the USB suppressed-carrier reference."""
    target_hz = parse_rf_frequency(target_frequency_hz)
    if not TXITY_RF_ROOM_MIN_HZ <= target_hz <= TXITY_RF_ROOM_MAX_HZ:
        raise ValueError("FreeDV RADE runs only in TXITY's 7.000-8.000 MHz room.")

    return "USB", target_hz


def apply_rade_transport(
    config: dict[str, Any],
    target_frequency_hz: Any,
) -> dict[str, Any]:
    """Apply the requested channel centre to the RADE SSB audio transport."""
    if str(config.get("_txitpyMode") or "").strip().upper() == "RADE":
        sideband, carrier_hz = rade_transport_settings(target_frequency_hz)
        config["mode"] = sideband
        config["rfHz"] = carrier_hz
        config["_txitpyRadeSideband"] = sideband
    return config


def build_frequency_candidates(
    primary_frequency: Any,
    fallback_frequencies: list[Any] | tuple[Any, ...] | None = None,
) -> list[tuple[str, int]]:
    """Return ordered, unique (display text, hertz) frequency candidates."""
    values = [primary_frequency, *(fallback_frequencies or [])]
    candidates: list[tuple[str, int]] = []
    seen: set[int] = set()

    for index, value in enumerate(values):
        display_text = str(value or "").strip()
        if not display_text:
            if index == 0:
                raise ValueError("Enter a valid primary frequency before starting transmission.")
            continue

        frequency_hz = parse_rf_frequency(display_text)
        if frequency_hz in seen:
            continue
        seen.add(frequency_hz)
        candidates.append((display_text, frequency_hz))

    if not candidates:
        raise ValueError("Enter at least one valid frequency.")
    return candidates


def mode_bandwidth_hz(
    mode: Any,
    station: dict[str, Any] | None = None,
    *,
    nfm_profile: str = NFM_DEFAULT_PROFILE,
) -> int:
    """Return a TXITY channel-width estimate, preferring station-reported values."""
    station = station if isinstance(station, dict) else {}
    mode_name = str(mode or "AM").strip().upper()
    if mode_name == "DRM [EXPERIMENTAL]":
        mode_name = "DRM"
    field_names = ["bandwidthHz", "occupiedBandwidthHz", "rfBandwidthHz"]
    if mode_name in {"AM", "DSB", "DRM"}:
        field_names.append("amBandwidthHz")
    elif mode_name in {"USB", "LSB", "USB+C", "LSB+C"}:
        field_names.append("ssbBandwidthHz")
    elif mode_name == "CW":
        field_names.append("cwBandwidthHz")

    for field_name in field_names:
        candidate = station.get(field_name)
        if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
            if math.isfinite(float(candidate)) and 0 < float(candidate) <= 2_000_000:
                return int(round(candidate))
    if mode_name == "NFM":
        return int(nfm_profile_settings(nfm_profile)["channel_bandwidth_hz"])
    return DEFAULT_MODE_BANDWIDTH_HZ.get(mode_name, DEFAULT_MODE_BANDWIDTH_HZ["AM"])



def find_frequency_overlaps(
    frequency: Any,
    mode: Any,
    stations: list[dict[str, Any]],
    *,
    nfm_profile: str = NFM_DEFAULT_PROFILE,
) -> list[dict[str, Any]]:
    """Find estimated channel overlaps with the supplied TXITY station records."""
    target_hz = parse_rf_frequency(frequency)
    target_width = mode_bandwidth_hz(mode, nfm_profile=nfm_profile)
    target_low = target_hz - target_width / 2
    target_high = target_hz + target_width / 2
    overlaps: list[dict[str, Any]] = []

    for station in stations:
        if not isinstance(station, dict):
            continue
        other_hz = None
        for field_name in ("frequency_hz", "frequencyHz", "rfHz"):
            candidate = station.get(field_name)
            if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
                if math.isfinite(float(candidate)) and candidate > 0:
                    other_hz = int(round(candidate))
                    break
        if other_hz is None:
            candidate = station.get("frequency")
            try:
                other_hz = parse_rf_frequency(candidate)
            except (TypeError, ValueError):
                continue

        other_mode = str(station.get("mode") or "AM").strip().upper()
        other_width = mode_bandwidth_hz(other_mode, station)
        other_low = other_hz - other_width / 2
        other_high = other_hz + other_width / 2
        overlap_width = min(target_high, other_high) - max(target_low, other_low)
        if overlap_width <= 0:
            continue

        overlaps.append({
            "station_id": str(station.get("id") or station.get("station_id") or ""),
            "station_name": str(station.get("station") or station.get("name") or "Unknown station"),
            "frequency_hz": other_hz,
            "mode": other_mode,
            "bandwidth_hz": other_width,
            "offset_hz": abs(target_hz - other_hz),
            "overlap_hz": int(round(overlap_width)),
            "allow_overlap_visitors": station.get("allow_overlap_visitors", station.get("allowOverlapVisitors"))
                if isinstance(station.get("allow_overlap_visitors", station.get("allowOverlapVisitors")), bool)
                else None,
        })

    return sorted(overlaps, key=lambda item: (item["offset_hz"], item["station_name"].casefold()))


def build_audio_socket_url(session_id: str, session_secret: str, auth_token: str) -> str:
    """Build the TXITY audio socket URL without logging its credentials."""
    if not session_id or not session_secret or not auth_token:
        raise ValueError("Sign in with Discord before opening the audio connection.")
    query = urlencode({
        "session": session_id,
        "sessionSecret": session_secret,
        "authToken": auth_token,
    })
    return f"{AUDIO_URL}?{query}"


def build_transmit_config(
    *,
    station_name: str,
    description: str,
    frequency: Any,
    mode: str,
    station_dbm: Any,
    allow_overlap: bool,
    auth_token: str,
    cw_text: str = "CQ CQ DE TXITY",
    cw_wpm: Any = 18,
    cw_loop: bool = True,
    station_logo_url: str = "",
    station_details: str = "",
    station_page_layout: str = "",
    fade_enabled: bool = False,
    fade_seconds: Any = 15,
    fade_min_dbm: Any = 25,
    fade_shape: Any = 0,
    agc_enabled: bool = True,
    agc_target: Any = 0.19,
    mic_gain_db: Any = 0,
    bass_db: Any = 0,
    treble_db: Any = 0,
    presence_db: Any = 0,
    drive: Any = 0.015,
    ssb_bandwidth_hz: Any = 10_000,
    nfm_transport_bandwidth_hz: Any = 12_000,
    ssb_low_cut_hz: Any = 325,
    ssb_high_cut_hz: Any = 0,
    am_bandwidth_hz: Any = 12_000,
    am_pre_emphasis: bool = False,
    drm_service_label: str = "",
    drm_service_id: str = "0xB30001",
    drm_language: str = "English",
    drm_programme_type: str = "No programme type",
    drm_iso_language: str = "",
    drm_iso_country: str = "",
    drm_audio_codec: str = "xHE-AAC",
    drm_msc_mode: str | None = None,
    drm_interleaving: str = "long",
    drm_quality_profile: str = DRM_DEFAULT_QUALITY_PROFILE,
    drm_stereo: bool | None = None,
    nfm_profile: str = NFM_DEFAULT_PROFILE,
) -> dict[str, Any]:
    """Create the TXITY config message using the website's currentConfig schema."""
    name = str(station_name or "").strip()
    if not name:
        raise ValueError("Enter a station name before starting transmission.")
    if len(name) > 48:
        raise ValueError("Station name must be 48 characters or fewer.")
    if not auth_token:
        raise ValueError("Sign in with Discord before starting transmission.")

    selected_mode = str(mode or "AM").strip().upper()
    if selected_mode == "DRM [EXPERIMENTAL]":
        selected_mode = "DRM"
    if selected_mode not in SUPPORTED_MODES:
        raise ValueError(f"Unsupported transmission mode: {selected_mode}.")
    if selected_mode == "DRM":
        server_mode = "DSB"
    else:
        server_mode = selected_mode
    server_frequency_hz = parse_rf_frequency(frequency)
    selected_nfm_profile = (
        nfm_profile_settings(nfm_profile)
        if selected_mode == "NFM"
        else None
    )
    selected_drm_profile = (
        drm_quality_profile_settings(drm_quality_profile)
        if selected_mode == "DRM"
        else None
    )
    if selected_mode == "NFM":
        server_mode, server_frequency_hz = nfm_transport_settings(
            server_frequency_hz,
            if_hz=selected_nfm_profile["if_hz"],
        )
    elif selected_mode == "RADE":
        server_mode, server_frequency_hz = rade_transport_settings(server_frequency_hz)

    try:
        power = float(station_dbm)
        words_per_minute = int(cw_wpm)
        fade_duration_seconds = float(fade_seconds)
        fade_floor = float(fade_min_dbm)
        fade_curve = int(fade_shape)
        agc_target_value = float(agc_target)
        mic_gain_value = float(mic_gain_db)
        bass_value = float(bass_db)
        treble_value = float(treble_db)
        presence_value = float(presence_db)
        drive_value = float(drive)
        nfm_transport_width = int(nfm_transport_bandwidth_hz)
        ssb_width = int(ssb_bandwidth_hz)
        ssb_low_cut = int(ssb_low_cut_hz)
        ssb_high_cut = int(ssb_high_cut_hz)
        am_width = int(am_bandwidth_hz)
    except (TypeError, ValueError) as exc:
        raise ValueError("Transmit settings must contain valid numeric values.") from exc

    numeric_values = (
        power, fade_duration_seconds, fade_floor, agc_target_value,
        mic_gain_value, bass_value, treble_value, presence_value, drive_value,
    )
    if not all(math.isfinite(value) for value in numeric_values):
        raise ValueError("Transmit settings must be finite numbers.")
    if not -140 <= power <= 53:
        raise ValueError("Station power must be between -140 and 53 dBm.")
    if not 5 <= words_per_minute <= 60:
        raise ValueError("CW speed must be between 5 and 60 WPM.")
    if not 10 <= fade_duration_seconds <= 60:
        raise ValueError("Fade cycle length must be between 10 and 60 seconds.")
    if not -140 <= fade_floor <= 50:
        raise ValueError("Fade minimum power must be between -140 and 50 dBm.")
    if not 0 <= fade_curve <= 5:
        raise ValueError("Fade style must be between 0 and 5.")
    if not 0.16 <= agc_target_value <= 0.24:
        raise ValueError("AGC target must be between 0.16 and 0.24 RMS.")
    if not -6 <= mic_gain_value <= 6:
        raise ValueError("Microphone gain must be between -6 and 6 dB.")
    if not -4 <= bass_value <= 4 or not -4 <= treble_value <= 4:
        raise ValueError("Bass and treble must be between -4 and 4 dB.")
    if not -1 <= presence_value <= 1:
        raise ValueError("Presence must be between -1 and 1 dB.")
    if not 0 <= drive_value <= 0.025:
        raise ValueError("Compression drive must be between 0 and 0.025.")
    if not 1_000 <= nfm_transport_width <= 20_000:
        raise ValueError("NFM transport bandwidth must be between 1000 and 20000 Hz.")
    if not 2_400 <= ssb_width <= 10_000:
        raise ValueError("SSB bandwidth must be between 2400 and 10000 Hz.")
    if not 0 <= ssb_low_cut <= 2_000:
        raise ValueError("SSB low cut must be between 0 and 2000 Hz.")
    if not 0 <= ssb_high_cut <= 3_000:
        raise ValueError("SSB high cut must be between 0 and 3000 Hz.")
    if not 3_000 <= am_width <= 12_000:
        raise ValueError("AM bandwidth must be between 3000 and 12000 Hz.")

    short_description = str(description or "").strip()[:180]
    configuration = {
        "type": "config",
        "rfHz": server_frequency_hz,
        "stationName": name,
        "stationDescription": short_description,
        "stationShortDescription": short_description,
        "stationLogoUrl": str(station_logo_url or "").strip()[:512],
        "stationDetails": str(station_details or "").strip()[:1_200],
        "stationPageLayout": str(station_page_layout or "")[:40_000],
        "discordAuthToken": auth_token,
        "mode": server_mode,
        "sampleRate": SAMPLE_RATE_HZ,
        "stationDbm": power,
        "fadeMs": int(round(fade_duration_seconds * 1_000)) if fade_enabled else 0,
        "fadeMinDbm": min(fade_floor, power),
        "fadeShape": fade_curve,
        "allowOverlapVisitors": bool(allow_overlap),
        "cwText": str(cw_text or "CQ CQ DE TXITY").strip()[:300],
        "cwWpm": words_per_minute,
        "cwLoop": bool(cw_loop),
        "agcEnabled": bool(agc_enabled),
        "agcTarget": agc_target_value,
        "micGainDb": mic_gain_value,
        "bassDb": bass_value,
        "trebleDb": treble_value,
        "presenceDb": presence_value,
        "drive": drive_value,
        "ssbBandwidthHz": ssb_width,
        "ssbLowCutHz": ssb_low_cut,
        "ssbHighCutHz": ssb_high_cut,
        "amBandwidthHz": 12_000 if selected_mode == "DRM" else am_width,
        "amPreEmphasis": False if selected_mode == "DRM" else bool(am_pre_emphasis),
    }
    if selected_mode == "DRM":
        assert selected_drm_profile is not None
        selected_drm_msc_mode = str(
            drm_msc_mode or selected_drm_profile["msc_mode"]
        ).strip().upper()
        selected_drm_stereo = (
            bool(selected_drm_profile["stereo"])
            if drm_stereo is None
            else bool(drm_stereo)
        )
        configuration["_txitpyMode"] = "DRM"
        configuration.update({
            "fadeMs": 0,
            "agcEnabled": False,
            "micGainDb": 0.0,
            "bassDb": 0.0,
            "trebleDb": 0.0,
            "presenceDb": 0.0,
            "drive": 0.0,
            "_txitpyDrmServiceLabel": str(drm_service_label or "").strip()[:16],
            "_txitpyDrmServiceId": str(drm_service_id or "0xB30001").strip(),
            "_txitpyDrmLanguage": str(drm_language or "English").strip(),
            "_txitpyDrmProgrammeType": str(drm_programme_type or "No programme type").strip(),
            "_txitpyDrmIsoLanguage": str(drm_iso_language or "").strip(),
            "_txitpyDrmIsoCountry": str(drm_iso_country or "").strip(),
            "_txitpyDrmAudioCodec": str(drm_audio_codec or "xHE-AAC").strip(),
            "_txitpyDrmMscMode": selected_drm_msc_mode,
            "_txitpyDrmInterleaving": str(drm_interleaving or "long").strip().casefold(),
            "_txitpyDrmQualityProfile": str(drm_quality_profile),
            "_txitpyDrmOccupancy": int(selected_drm_profile["occupancy"]),
            "_txitpyDrmStereo": selected_drm_stereo,
        })
    elif selected_mode == "RADE":
        configuration.update({
            "_txitpyMode": "RADE",
            "_txitpyRadeCenterHz": RADE_AUDIO_CENTER_HZ,
            "_txitpyRadeChannelBandwidthHz": RADE_CHANNEL_BANDWIDTH_HZ,
            "fadeMs": 0,
            "agcEnabled": False,
            "micGainDb": 0.0,
            "bassDb": 0.0,
            "trebleDb": 0.0,
            "presenceDb": 0.0,
            "drive": 0.0,
            "ssbBandwidthHz": 3_000,
            "ssbLowCutHz": 0,
            "ssbHighCutHz": 0,
        })
    elif selected_mode == "NFM":
        assert selected_nfm_profile is not None
        configuration.update({
            "_txitpyMode": "NFM",
            "_txitpyNfmProfile": str(nfm_profile),
            "_txitpyNfmIfHz": int(selected_nfm_profile["if_hz"]),
            "_txitpyNfmDeviationHz": int(selected_nfm_profile["deviation_hz"]),
            "_txitpyNfmInputLowCutHz": int(selected_nfm_profile["audio_low_hz"]),
            "_txitpyNfmInputHighCutHz": int(selected_nfm_profile["audio_high_hz"]),
            "_txitpyNfmChannelBandwidthHz": int(selected_nfm_profile["channel_bandwidth_hz"]),
            "_txitpyNfmInputGainDb": mic_gain_value,
            "_txitpyNfmSideband": server_mode,
            "fadeMs": 0,
            "agcEnabled": False,
            "micGainDb": 0.0,
            "bassDb": 0.0,
            "trebleDb": 0.0,
            "presenceDb": 0.0,
            "drive": 0.0,
            "ssbBandwidthHz": nfm_transport_width,
            "ssbLowCutHz": 0,
            "ssbHighCutHz": 0,
        })
    return configuration


def _is_linux_monitor_device(device: dict[str, Any]) -> bool:
    """Identify PulseAudio and PipeWire monitor sources exposed as input devices."""
    name = str(device.get("name") or "").casefold()
    return "monitor" in name


def list_audio_inputs() -> list[tuple[int, str]]:
    """Return microphone and line-input devices, excluding Linux monitor sources."""
    import sounddevice

    devices = sounddevice.query_devices()
    return [
        (index, str(device.get("name") or f"Audio input {index}"))
        for index, device in enumerate(devices)
        if int(device.get("max_input_channels", 0)) > 0
        and not (
            platform.system().casefold() == "linux"
            and _is_linux_monitor_device(device)
        )
    ]


def list_loopback_devices() -> list[tuple[int, str]]:
    """Return desktop-audio loopback devices for the current operating system."""
    if platform.system().casefold() == "linux":
        import sounddevice

        devices = sounddevice.query_devices()
        host_apis = sounddevice.query_hostapis()
        result: list[tuple[int, str]] = []
        for index, device in enumerate(devices):
            if int(device.get("max_input_channels", 0)) < 1:
                continue
            if not _is_linux_monitor_device(device):
                continue
            host_api_index = int(device.get("hostapi", -1))
            host_api_name = ""
            if 0 <= host_api_index < len(host_apis):
                host_api_name = str(host_apis[host_api_index].get("name") or "")
            name = str(device.get("name") or f"Desktop monitor {index}")
            label = f"{index}: {name}"
            if host_api_name:
                label += f" [{host_api_name}]"
            result.append((index, label))

        if not result:
            raise RuntimeError(
                "No desktop-audio monitor sources were found. On Linux, enable a "
                "PulseAudio or PipeWire monitor source, then refresh audio devices."
            )
        return result

    if os.name != "nt":
        raise RuntimeError(
            "Desktop-audio loopback is supported on Windows and Linux. "
            "Linux requires a PulseAudio or PipeWire monitor source."
        )

    try:
        import pyaudiowpatch as pyaudio
    except ImportError as exc:
        raise RuntimeError(
            "Desktop audio capture requires PyAudioWPatch. Install the project requirements and reopen MultiV."
        ) from exc

    manager = pyaudio.PyAudio()
    try:
        try:
            default_info = manager.get_default_wasapi_loopback()
            default_index = int(default_info["index"])
        except (OSError, KeyError, TypeError, ValueError):
            default_index = None

        devices: list[tuple[int, str]] = []
        for info in manager.get_loopback_device_info_generator():
            try:
                index = int(info["index"])
                name = str(info.get("name") or f"Desktop output {index}")
            except (KeyError, TypeError, ValueError):
                continue
            label = f"{index}: {name}"
            if index == default_index:
                label += " (Default output)"
            devices.append((index, label))

        if default_index is not None:
            devices.sort(key=lambda item: (item[0] != default_index, item[0]))
        if not devices:
            raise RuntimeError(
                "No Windows WASAPI loopback devices were found. Check that a playback device is enabled."
            )
        return devices
    finally:
        manager.terminate()


def _safe_error(error: BaseException) -> str:
    """Avoid including authenticated WebSocket query strings in error text."""
    text = str(error)
    text = re.sub(r"(?i)wss?://[^\s]+", "[WebSocket URL redacted]", text)
    text = re.sub(r"(?i)(authToken|sessionSecret)=([^&\s]+)", r"\1=[redacted]", text)
    return f"{type(error).__name__}: {text[:700]}"


def _safe_traceback(error: BaseException) -> str:
    """Format a full traceback while removing authenticated WebSocket URLs and secrets."""
    details = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    details = re.sub(r"(?i)wss?://[^\s\"'<>]+", "[WebSocket URL redacted]", details)
    details = re.sub(
        r"(?i)(authToken|sessionSecret)(=|:\s*)([^&\s,\"'<>]+)",
        r"\1\2[redacted]",
        details,
    )
    return details.rstrip()


class StreamingLinearResampler:
    """Resample mono float audio while retaining phase between callback blocks."""

    def __init__(self, source_rate: int, target_rate: int = SAMPLE_RATE_HZ) -> None:
        if source_rate <= 0 or target_rate <= 0:
            raise ValueError("Audio sample rates must be positive.")
        self.source_rate = int(source_rate)
        self.target_rate = int(target_rate)
        self.step = self.source_rate / self.target_rate
        self._buffer = None
        self._position = 0.0

    def process(self, samples):
        import numpy as np

        chunk = np.asarray(samples, dtype=np.float32).reshape(-1)
        if chunk.size == 0:
            return np.empty(0, dtype=np.float32)
        if self.source_rate == self.target_rate:
            return chunk.copy()

        combined = (
            chunk
            if self._buffer is None
            else np.concatenate((self._buffer, chunk))
        )
        if combined.size < 2:
            self._buffer = combined.copy()
            return np.empty(0, dtype=np.float32)

        positions = np.arange(
            self._position,
            combined.size - 1,
            self.step,
            dtype=np.float64,
        )
        if positions.size == 0:
            self._buffer = combined.copy()
            return np.empty(0, dtype=np.float32)

        left = positions.astype(np.int64)
        fraction = (positions - left).astype(np.float32)
        output = combined[left] + (
            combined[left + 1] - combined[left]
        ) * fraction

        next_position = self._position + positions.size * self.step
        discard = max(0, min(int(next_position) - 1, combined.size - 1))
        self._buffer = combined[discard:].copy()
        self._position = next_position - discard
        return np.asarray(output, dtype=np.float32)



class TxityTransmitter:
    """Stream microphone or desktop-loopback PCM to TXITY."""

    def __init__(self, client: Any, events: queue.Queue[dict[str, Any]]) -> None:
        self.client = client
        self.events = events
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._starting = False
        self._active = False
        self._ptt_sent = False
        self._ptt_requested_at: float | None = None
        self._audio_queue: queue.Queue[Any] = queue.Queue(maxsize=AUDIO_QUEUE_FRAMES)

    @property
    def is_busy(self) -> bool:
        with self._lock:
            return self._starting or self._active

    @property
    def server_rejection_ready(self) -> bool:
        """Ignore stale negative status snapshots immediately after sending PTT."""
        with self._lock:
            requested_at = self._ptt_requested_at
            return bool(
                self._active
                and self._ptt_sent
                and not self._stop_event.is_set()
                and requested_at is not None
                and time.monotonic() - requested_at >= PTT_CONFIRMATION_GRACE_SECONDS
            )

    def start(
        self,
        config: dict[str, Any],
        input_device: int | None = None,
        audio_source: str = "mic",
        playlist_files: list[str] | None = None,
        playlist_shuffle: bool = False,
        playlist_loop: bool = False,
        icecast_destinations: list[dict[str, Any]] | None = None,
        icecast_bitrate_kbps: int = 128,
        icecast_server_settings: dict[str, Any] | None = None,
    ) -> None:
        selected_source = str(audio_source or "mic").casefold()
        if selected_source not in SUPPORTED_AUDIO_SOURCES:
            raise ValueError("Choose microphone input, desktop audio loopback, or an audio playlist.")
        drm_mode = str(config.get("_txitpyMode") or "").upper() == "DRM"
        rade_mode = str(config.get("_txitpyMode") or "").upper() == "RADE"
        if drm_mode and selected_source == "loopback":
            raise ValueError("DRM mode supports microphone and playlist inputs. It does not support desktop audio loopback.")
        selected_playlist = collect_audio_files(playlist_files or [])
        if selected_source == "playlist" and not selected_playlist:
            raise ValueError("Add at least one supported audio file to the playlist.")
        selected_icecast_destinations = [
            normalize_icecast_destination(item)
            for item in (icecast_destinations or [])
        ]
        try:
            selected_icecast_bitrate = int(icecast_bitrate_kbps)
        except (TypeError, ValueError) as exc:
            raise ValueError("Icecast bitrate must be a whole number.") from exc
        if selected_icecast_bitrate not in ICECAST_BITRATES:
            raise ValueError(
                "Icecast bitrate must be one of: "
                + ", ".join(str(value) for value in ICECAST_BITRATES)
                + " kbps."
            )

        with self._lock:
            if self._starting or self._active or (self._thread and self._thread.is_alive()):
                raise RuntimeError("A transmission is already starting or active.")
            self._stop_event.clear()
            queue_frames = (
                PLAYLIST_AUDIO_QUEUE_FRAMES
                if selected_source == "playlist" or drm_mode
                else RADE_AUDIO_QUEUE_FRAMES if rade_mode
                else AUDIO_QUEUE_FRAMES
            )
            self._audio_queue = queue.Queue(maxsize=queue_frames)
            self._starting = True
            self._active = False
            self._ptt_sent = False
            self._ptt_requested_at = None
            self._thread = threading.Thread(
                target=self._run,
                args=(
                    dict(config),
                    input_device,
                    selected_source,
                    selected_playlist,
                    bool(playlist_shuffle),
                    bool(playlist_loop),
                    selected_icecast_destinations,
                    selected_icecast_bitrate,
                    dict(icecast_server_settings or {}),
                ),
                daemon=True,
                name="txity-transmitter",
            )
            self.events.put({
                "kind": "tx_state",
                "state": "starting",
                "text": "Preparing the TXITY transmission connection...",
            })
            self._thread.start()

    def stop(self, wait: float = 0.0) -> None:
        with self._lock:
            thread = self._thread
            busy = self._starting or self._active
            self._stop_event.set()
        if busy:
            self.events.put({
                "kind": "tx_state",
                "state": "stopping",
                "text": "Stopping transmission...",
            })
        if wait > 0 and thread and thread is not threading.current_thread():
            thread.join(timeout=wait)

    def _run(
        self,
        config: dict[str, Any],
        input_device: int | None,
        audio_source: str,
        playlist_files: list[str],
        playlist_shuffle: bool,
        playlist_loop: bool,
        icecast_destinations: list[dict[str, Any]],
        icecast_bitrate_kbps: int,
        icecast_server_settings: dict[str, Any],
    ) -> None:
        import numpy as np
        import websocket

        system_awake_handle = begin_windows_system_awake()
        mode = str(config.get("mode", "AM")).upper()
        drm_mode = str(config.get("_txitpyMode") or "").upper() == "DRM"
        nfm_mode = str(config.get("_txitpyMode") or "").upper() == "NFM"
        rade_mode = str(config.get("_txitpyMode") or "").upper() == "RADE"
        needs_audio = mode != "CW"
        audio_socket = None
        input_stream = None
        loopback_stream = None
        loopback_manager = None
        playlist_thread: threading.Thread | None = None
        drm_process: subprocess.Popen | None = None
        drm_playlist_server: LocalDRMPlaylistServer | None = None
        drm_temp_dir = None
        drm_stop_path: Path | None = None
        drm_reader_thread: threading.Thread | None = None
        drm_stderr_thread: threading.Thread | None = None
        drm_reader_finished = threading.Event()
        drm_stderr_lines: list[str] = []
        drm_playlist_stop_deadline: float | None = None
        drm_playlist_stop_requested = False
        audio_finished = threading.Event()
        playlist_reached_end = False
        playlist_underruns = 0
        playlist_underrun_active = False
        playlist_next_frame_deadline: float | None = None
        playlist_current_track: tuple[str, int, int] | None = None
        audio_timer_resolution = None
        ptt_sent = False
        audio_thread_error: list[str] = []
        audio_capture_error: list[str] = []
        audio_queue = self._audio_queue
        icecast_broadcaster: IcecastBroadcaster | None = None
        local_icecast_server = None
        nfm_modulator = (
            StreamingNFMModulator(
                if_hz=int(config.get("_txitpyNfmIfHz", NFM_IF_HZ)),
                deviation_hz=int(config.get("_txitpyNfmDeviationHz", NFM_DEVIATION_HZ)),
                low_cut_hz=int(config.get("_txitpyNfmInputLowCutHz", NFM_AUDIO_LOW_HZ)),
                high_cut_hz=int(config.get("_txitpyNfmInputHighCutHz", NFM_AUDIO_HIGH_HZ)),
                input_gain_db=float(config.get("_txitpyNfmInputGainDb", 0.0)),
            )
            if nfm_mode else None
        )
        nfm_modulator_lock = threading.Lock()
        rade_encoder: FreeDVRadeEncoder | None = None
        rade_finalized = False
        rade_playlist_info: tuple[str, int, int] | None = None
        rade_playlist_info_lock = threading.Lock()
        last_audio_frame_at = time.monotonic()
        last_capture_error_at = 0.0
        control_configured = False
        control_disconnect_reported = False
        last_control_generation: int | None = None
        transmission_announced = False
        audio_reconnect_attempt = 0
        pending_frame: Any = None
        carry = bytearray()
        last_callback_warning = 0.0
        audio_frames_sent = 0
        last_audio_progress_at = time.monotonic()

        def modulate_nfm_samples(samples):
            if nfm_modulator is None:
                return samples
            with nfm_modulator_lock:
                return nfm_modulator.process(samples)

        def enqueue_transport_audio_samples(
            samples,
            *,
            wait_for_space: bool = False,
            flush: bool = False,
            playlist_info: tuple[str, int, int] | None = None,
        ) -> None:
            import numpy as np

            pcm = np.asarray(samples, dtype="<f4").reshape(-1)
            if pcm.size:
                carry.extend(pcm.tobytes())
            if flush and carry and len(carry) < FRAME_BYTES:
                carry.extend(bytes(FRAME_BYTES - len(carry)))

            while len(carry) >= FRAME_BYTES:
                frame = bytes(carry[:FRAME_BYTES])
                del carry[:FRAME_BYTES]
                queued_frame = (
                    (frame, playlist_info)
                    if audio_source == "playlist"
                    else frame
                )
                if wait_for_space and not self._stop_event.is_set():
                    while not self._stop_event.is_set():
                        try:
                            audio_queue.put(queued_frame, timeout=0.1)
                            break
                        except queue.Full:
                            continue
                    continue

                try:
                    audio_queue.put_nowait(queued_frame)
                except queue.Full:
                    try:
                        audio_queue.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        audio_queue.put_nowait(queued_frame)
                    except queue.Full:
                        pass

        def enqueue_rade_output(samples) -> None:
            with rade_playlist_info_lock:
                playlist_info = rade_playlist_info
            enqueue_transport_audio_samples(
                samples,
                wait_for_space=(
                    audio_source == "playlist"
                    and not self._stop_event.is_set()
                ),
                playlist_info=playlist_info,
            )

        def enqueue_pcm_samples(
            samples,
            *,
            wait_for_space: bool = False,
            flush: bool = False,
            playlist_info: tuple[str, int, int] | None = None,
        ) -> None:
            nonlocal rade_playlist_info
            import numpy as np

            pcm = np.asarray(samples, dtype=np.float32).reshape(-1)
            if pcm.size and icecast_broadcaster is not None:
                icecast_broadcaster.send_pcm(
                    pcm,
                    wait_for_space=wait_for_space,
                )
            if rade_encoder is not None:
                if playlist_info is not None:
                    with rade_playlist_info_lock:
                        rade_playlist_info = playlist_info
                if pcm.size:
                    rade_encoder.feed(pcm, wait_for_space=wait_for_space)
                return

            if pcm.size and nfm_modulator is not None:
                pcm = modulate_nfm_samples(pcm)
            enqueue_transport_audio_samples(
                pcm,
                wait_for_space=wait_for_space,
                flush=flush,
                playlist_info=playlist_info,
            )

        def report_audio_warning(status: Any) -> None:
            nonlocal last_callback_warning
            if not status:
                return
            now = time.monotonic()
            if now - last_callback_warning >= 5.0:
                last_callback_warning = now
                self.events.put({
                    "kind": "tx_audio_warning",
                    "text": str(status)[:300],
                })

        def microphone_callback(indata, _frames, _time_info, status) -> None:
            report_audio_warning(status)
            try:
                enqueue_pcm_samples(indata[:, 0])
            except Exception as exc:
                audio_capture_error[:] = [_safe_error(exc)]

        def loopback_callback_factory(channels: int, source_rate: int, pyaudio):
            import numpy as np

            resampler = StreamingLinearResampler(source_rate, SAMPLE_RATE_HZ)

            def callback(in_data, _frame_count, _time_info, status):
                report_audio_warning(status)
                try:
                    raw = np.frombuffer(in_data or b"", dtype=np.float32)
                    if raw.size:
                        usable = raw.size - (raw.size % channels)
                        if usable <= 0:
                            return (in_data, pyaudio.paContinue)
                        audio = raw[:usable].reshape(-1, channels)
                        mono = audio.mean(axis=1, dtype=np.float32) if channels > 1 else audio[:, 0]
                        enqueue_pcm_samples(resampler.process(mono))
                except Exception as exc:
                    audio_capture_error[:] = [_safe_error(exc)]
                return (None, pyaudio.paContinue)

            return callback

        def start_playlist_worker() -> threading.Thread:
            def stream_playlist() -> None:
                previous_file: str | None = None
                rng = random.Random()
                try:
                    import numpy as np
                    import soundfile

                    while not self._stop_event.is_set():
                        order = build_playlist_order(
                            playlist_files,
                            shuffle=playlist_shuffle,
                            previous_file=previous_file,
                            rng=rng,
                        )
                        for index, file_path in enumerate(order, start=1):
                            if self._stop_event.is_set():
                                break
                            playlist_info = (file_path, index, len(order))
                            source_rate = 0
                            resampler = None
                            for block_rate, block in iter_playlist_audio_blocks(
                                file_path,
                                PLAYLIST_READ_FRAMES,
                            ):
                                if self._stop_event.is_set():
                                    break
                                if resampler is None or block_rate != source_rate:
                                    if resampler is not None:
                                        enqueue_pcm_samples(
                                            [],
                                            wait_for_space=True,
                                            flush=True,
                                            playlist_info=playlist_info,
                                        )
                                    source_rate = block_rate
                                    resampler = StreamingLinearResampler(
                                        source_rate,
                                        SAMPLE_RATE_HZ,
                                    )

                                mono = block.mean(axis=1, dtype=np.float32)
                                enqueue_pcm_samples(
                                    resampler.process(mono),
                                    wait_for_space=True,
                                    playlist_info=playlist_info,
                                )

                            if not self._stop_event.is_set():
                                if resampler is None:
                                    raise RuntimeError(
                                        f"Audio file contains no playable samples: {file_path}"
                                    )
                                enqueue_pcm_samples(
                                    [],
                                    wait_for_space=True,
                                    flush=True,
                                    playlist_info=playlist_info,
                                )
                            previous_file = file_path

                        if self._stop_event.is_set() or not playlist_loop:
                            break

                    if not self._stop_event.is_set():
                        enqueue_pcm_samples(
                            [],
                            wait_for_space=True,
                            flush=True,
                        )
                except Exception as exc:
                    details = _safe_error(exc)
                    audio_thread_error[:] = [details]
                    self.events.put({
                        "kind": "tx_playlist_error",
                        "text": details,
                    })
                    self.events.put({
                        "kind": "tx_log",
                        "text": f"Playlist decoder error: {details}",
                    })
                finally:
                    audio_finished.set()

            thread = threading.Thread(
                target=stream_playlist,
                daemon=True,
                name="txitpy-playlist",
            )
            thread.start()
            return thread

        server_config = {
            key: value for key, value in config.items()
            if not key.startswith("_txitpy")
        }
        control_endpoints = [("TXITY", self.client, server_config)]
        self.events.put({
            "kind": "tx_log",
            "text": (
                "TXITY config request: "
                f"rfHz={server_config.get('rfHz')}, "
                f"mode={server_config.get('mode')}, "
                f"allowOverlapVisitors={server_config.get('allowOverlapVisitors')}."
            ),
        })
        control_wait_reported = False

        def wait_for_control_connection() -> bool:
            nonlocal control_wait_reported
            while not self._stop_event.is_set():
                if self.client.control_connected():
                    if control_wait_reported:
                        self.events.put({
                            "kind": "tx_log",
                            "text": "TXITY control connection restored.",
                        })
                    control_wait_reported = False
                    return True
                if not control_wait_reported:
                    self.events.put({
                        "kind": "tx_log",
                        "text": "TXITY control connection unavailable; waiting before sending audio.",
                    })
                    control_wait_reported = True
                self._stop_event.wait(0.5)
            return False

        def open_audio_connection(target_client: Any, label: str) -> Any:
            attempt = 0
            while not self._stop_event.is_set():
                if not wait_for_control_connection():
                    return None
                try:
                    session_id, session_secret, auth_token = target_client.transmission_credentials()
                    current_audio_url = build_audio_socket_url(
                        session_id,
                        session_secret,
                        auth_token,
                    )
                    connection = websocket.create_connection(
                        current_audio_url,
                        timeout=10,
                        origin=BASE_URL,
                        enable_multithread=True,
                    )
                    connection.settimeout(1.0)
                    if self._stop_event.is_set():
                        connection.close()
                        return None
                    self.events.put({
                        "kind": "tx_log",
                        "text": f"Authenticated TX audio WebSocket connected for {label}.",
                    })
                    return connection
                except Exception as exc:
                    attempt += 1
                    delay = reconnect_delay(attempt)
                    self.events.put({
                        "kind": "tx_log",
                        "text": (
                            f"{label} TXITY audio connection failed ({_safe_error(exc)}). "
                            f"Retrying in {delay:.0f} seconds."
                        ),
                    })
                    self._stop_event.wait(delay)
            return None

        def audio_connection_is_open(connection: Any) -> bool:
            if connection is None:
                return False
            state = getattr(connection, "connected", None)
            if state is None:
                state = getattr(getattr(connection, "sock", None), "connected", False)
            return bool(state)

        def close_audio_connection() -> None:
            nonlocal audio_socket
            if audio_socket is not None:
                try:
                    audio_socket.close()
                except Exception:
                    pass
            audio_socket = None

        def audio_socket_is_available_for_drain(connection: Any) -> bool:
            return audio_connection_is_open(connection)


        try:
            if self._stop_event.is_set():
                return

            icecast_broadcaster: IcecastBroadcaster | None = None
            local_icecast_server = None
            if icecast_destinations or bool(icecast_server_settings.get("enabled")):
                if drm_mode or mode == "CW":
                    unsupported_mode = "DRM" if drm_mode else "CW"
                    self.events.put({
                        "kind": "tx_icecast_state",
                        "name": "Icecast",
                        "state": "Unavailable",
                        "text": f"Icecast output is not available in {unsupported_mode} mode.",
                    })
                else:
                    if bool(icecast_server_settings.get("enabled")):
                        local_icecast_server = EmbeddedIcecastServer(
                            host=str(icecast_server_settings.get("host") or "0.0.0.0"),
                            port=int(icecast_server_settings.get("port", 8000)),
                            mount=str(icecast_server_settings.get("mount") or "/live.mp3"),
                            name=str(config.get("stationName") or "MultiV"),
                            description=str(config.get("stationDescription") or ""),
                            listener_password=str(icecast_server_settings.get("listener_password") or ""),
                            max_listeners=int(icecast_server_settings.get("max_listeners", 25)),
                            bitrate_kbps=icecast_bitrate_kbps,
                            events=self.events,
                        )
                    icecast_broadcaster = IcecastBroadcaster(
                        icecast_destinations,
                        bitrate_kbps=icecast_bitrate_kbps,
                        station_name=str(config.get("stationName") or "MultiV"),
                        station_description=str(config.get("stationDescription") or ""),
                        events=self.events,
                        sample_rate=SAMPLE_RATE_HZ,
                        local_server=local_icecast_server,
                    )
                    icecast_broadcaster.start()

            drm_config_path: Path | None = None
            encoder_path: Path | None = None
            if rade_mode:
                self.events.put({
                    "kind": "tx_log",
                    "text": "Preparing FreeDV RADE V1 for the selected TXITY sideband channel.",
                })
                rade_executable = prepare_rade_encoder(
                    on_log=lambda message: self.events.put({
                        "kind": "tx_log",
                        "text": str(message)[:500],
                    })
                )
                rade_encoder = FreeDVRadeEncoder(
                    rade_executable,
                    on_audio=enqueue_rade_output,
                )
                self.events.put({
                    "kind": "tx_log",
                    "text": (
                        f"FreeDV RADE V1 encoder ready: {RADE_AUDIO_CENTER_HZ} Hz audio centre, "
                        f"{RADE_CHANNEL_BANDWIDTH_HZ} Hz channel bandwidth."
                    ),
                })
            if nfm_mode:
                sideband = str(config.get("_txitpyNfmSideband") or mode)
                self.events.put({
                    "kind": "tx_log",
                    "text": (
                        f"Software NFM active: {NFM_DEVIATION_HZ} Hz peak deviation, "
                        f"{NFM_AUDIO_LOW_HZ}-{NFM_AUDIO_HIGH_HZ} Hz voice band, "
                        f"{NFM_IF_HZ} Hz IF via {sideband} transport."
                    ),
                })
            if drm_mode:
                self.events.put({
                    "kind": "tx_log",
                    "text": "Preparing the DRM30 encoder and one-service channel.",
                })
                if audio_source == "loopback":
                    raise RuntimeError(
                        "DRM mode supports microphone and playlist inputs. It does not support desktop audio loopback."
                    )
                encoder_path = ensure_drm_encoder()
                drm_temp_dir = tempfile.TemporaryDirectory(prefix="txitpy-drm-")
                drm_temp_path = Path(drm_temp_dir.name)
                drm_stop_path = drm_temp_path / "stop"
                drm_metadata = {
                    "service_label": config.get("_txitpyDrmServiceLabel") or None,
                    "service_id": config.get("_txitpyDrmServiceId", "0xB30001"),
                    "language": config.get("_txitpyDrmLanguage", "English"),
                    "programme_type": config.get("_txitpyDrmProgrammeType", "No programme type"),
                    "iso_language": config.get("_txitpyDrmIsoLanguage", ""),
                    "iso_country": config.get("_txitpyDrmIsoCountry", ""),
                    "audio_codec": config.get("_txitpyDrmAudioCodec", "xHE-AAC"),
                    "msc_mode": config.get("_txitpyDrmMscMode", "16-QAM"),
                    "interleaving": config.get("_txitpyDrmInterleaving", "long"),
                    "occupancy": int(config.get("_txitpyDrmOccupancy", 0)),
                    "stereo": bool(config.get("_txitpyDrmStereo", False)),
                }

                if audio_source == "playlist":
                    supported = [
                        item for item in playlist_files
                        if Path(item).suffix.casefold() in {".ogg", ".mp3", ".flac"}
                    ]
                    if len(supported) != len(playlist_files):
                        self.events.put({
                            "kind": "tx_log",
                            "text": "DRM skips WAV and AIFF playlists. Use OGG, MP3, or FLAC files.",
                        })
                    drm_playlist_server = LocalDRMPlaylistServer(
                        supported,
                        shuffle=playlist_shuffle,
                        loop=playlist_loop,
                        on_track=lambda path, index, count: self.events.put({
                            "kind": "tx_playlist_track",
                            "path": path,
                            "index": index,
                            "count": count,
                        }),
                    )
                    drm_config_path = write_drm_station_config(
                        drm_temp_path / "station.toml",
                        station_name=config.get("stationName", "MultiV"),
                        playlist_url=drm_playlist_server.url,
                        **drm_metadata,
                    )
                else:
                    import sounddevice

                    if input_device is None:
                        input_name = "default"
                    else:
                        input_info = sounddevice.query_devices(input_device, "input")
                        input_name = str(input_info.get("name") or "").strip()
                        if not input_name:
                            raise RuntimeError("The selected microphone does not have a usable device name.")
                    drm_config_path = write_drm_station_config(
                        drm_temp_path / "station.toml",
                        station_name=config.get("stationName", "MultiV"),
                        input_device_name=input_name,
                        **drm_metadata,
                    )

            self.client.transmission_credentials()

            if not wait_for_control_connection():
                return
            last_control_generation = getattr(
                self.client, "control_generation", None
            )

            if needs_audio:
                audio_socket = open_audio_connection(self.client, "TXITY")
                if audio_socket is None:
                    return

                if audio_source == "loopback" and not drm_mode:
                    if input_device is None:
                        raise RuntimeError("Select a desktop-audio monitor device for loopback capture.")

                    if platform.system().casefold() == "linux":
                        import sounddevice

                        input_stream = sounddevice.InputStream(
                            device=input_device,
                            samplerate=SAMPLE_RATE_HZ,
                            channels=1,
                            dtype="float32",
                            blocksize=BATCH_SAMPLES,
                            latency="low",
                            callback=microphone_callback,
                        )
                        input_stream.start()
                        self.events.put({
                            "kind": "tx_log",
                            "text": (
                                "Desktop-audio monitor opened through PortAudio; "
                                "capturing 48 kHz mono Float32."
                            ),
                        })
                    else:
                        try:
                            import pyaudiowpatch as pyaudio
                        except ImportError as exc:
                            raise RuntimeError(
                                "Desktop audio capture requires PyAudioWPatch. Install the project requirements and reopen MultiV."
                            ) from exc

                        loopback_manager = pyaudio.PyAudio()
                        info = loopback_manager.get_device_info_by_index(input_device)
                        if not bool(info.get("isLoopbackDevice")):
                            raise RuntimeError("The selected device is not a Windows WASAPI loopback input.")

                        channels = min(2, int(info.get("maxInputChannels", 0)))
                        source_rate = int(round(float(info.get("defaultSampleRate", SAMPLE_RATE_HZ))))
                        if channels < 1 or source_rate < 1:
                            raise RuntimeError("The selected desktop output does not expose a usable audio format.")

                        loopback_stream = loopback_manager.open(
                            format=pyaudio.paFloat32,
                            channels=channels,
                            rate=source_rate,
                            input=True,
                            input_device_index=input_device,
                            frames_per_buffer=BATCH_SAMPLES,
                            stream_callback=loopback_callback_factory(channels, source_rate, pyaudio),
                            start=False,
                        )
                        loopback_stream.start_stream()
                        self.events.put({
                            "kind": "tx_log",
                            "text": (
                                f"Desktop output loopback opened: {info.get('name', 'selected output')} "
                                f"({source_rate} Hz, {channels} channels); downmixing and converting to "
                                "48 kHz mono Float32."
                            ),
                        })
                elif audio_source == "mic" and not drm_mode:
                    import sounddevice

                    input_stream = sounddevice.InputStream(
                        device=input_device,
                        samplerate=SAMPLE_RATE_HZ,
                        channels=1,
                        dtype="float32",
                        blocksize=BATCH_SAMPLES,
                        latency="low",
                        callback=microphone_callback,
                    )
                    input_stream.start()
                    self.events.put({
                        "kind": "tx_log",
                        "text": "Microphone opened at 48 kHz, mono Float32; sending 512-sample batches.",
                    })

            if needs_audio and audio_source == "playlist" and not drm_mode:
                audio_timer_resolution = begin_windows_audio_timer_resolution()
                playlist_thread = start_playlist_worker()
                self.events.put({
                    "kind": "tx_log",
                    "text": (
                        "Buffering FreeDV RADE output before starting transmission."
                        if rade_mode
                        else "Buffering a few seconds of playlist audio before starting transmission."
                    ),
                })
                required_frames = RADE_PREROLL_FRAMES if rade_mode else PLAYLIST_PREROLL_FRAMES
                buffer_deadline = time.monotonic() + 12.0
                while not self._stop_event.is_set():
                    if audio_thread_error:
                        raise RuntimeError(audio_thread_error[0])
                    if rade_encoder is not None and rade_encoder.error:
                        raise RuntimeError(rade_encoder.error)
                    if audio_queue.qsize() >= required_frames or audio_finished.is_set():
                        break
                    if time.monotonic() >= buffer_deadline:
                        raise TimeoutError(
                            "Playlist audio did not fill its startup buffer within 12 seconds."
                        )
                    self._stop_event.wait(0.02)

                if self._stop_event.is_set():
                    return
                if rade_mode and audio_finished.is_set() and rade_encoder is not None:
                    rade_encoder.finish()
                    rade_finalized = True
                    if rade_encoder.error:
                        raise RuntimeError(rade_encoder.error)
                    enqueue_transport_audio_samples(
                        [],
                        flush=True,
                        playlist_info=rade_playlist_info,
                    )
                if audio_queue.empty():
                    raise RuntimeError("The playlist did not produce any playable audio samples.")
                self.events.put({
                    "kind": "tx_log",
                    "text": (
                        f"Playlist buffer ready: {audio_queue.qsize()} audio frames "
                        f"({audio_queue.qsize() * AUDIO_FRAME_INTERVAL_SECONDS:.2f} seconds)."
                    ),
                })

            if rade_mode and audio_source != "playlist":
                self.events.put({
                    "kind": "tx_log",
                    "text": "Buffering the FreeDV RADE signal before keying TXITY.",
                })
                buffer_deadline = time.monotonic() + 12.0
                while not self._stop_event.is_set():
                    if audio_capture_error:
                        raise RuntimeError(audio_capture_error[-1])
                    if rade_encoder is not None and rade_encoder.error:
                        raise RuntimeError(rade_encoder.error)
                    if audio_queue.qsize() >= RADE_PREROLL_FRAMES:
                        break
                    if time.monotonic() >= buffer_deadline:
                        raise TimeoutError(
                            "FreeDV RADE did not produce enough audio to start the transmission within 12 seconds."
                        )
                    self._stop_event.wait(0.02)
                if self._stop_event.is_set():
                    return
                self.events.put({
                    "kind": "tx_log",
                    "text": (
                        f"FreeDV RADE signal buffer ready: {audio_queue.qsize()} audio frames."
                    ),
                })

            if drm_mode:
                if drm_config_path is None or drm_stop_path is None or encoder_path is None:
                    raise RuntimeError("The DRM encoder configuration was not prepared.")
                audio_timer_resolution = begin_windows_audio_timer_resolution()
                drm_process = subprocess.Popen(
                    [str(encoder_path), str(drm_config_path), str(drm_stop_path)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=0,
                )
                process = drm_process

                def read_drm_stdout() -> None:
                    import numpy as np

                    raw_carry = bytearray()
                    try:
                        assert process.stdout is not None
                        while True:
                            chunk = process.stdout.read(65_536)
                            if not chunk:
                                break
                            raw_carry.extend(chunk)
                            usable = len(raw_carry) - (len(raw_carry) % 4)
                            if usable:
                                samples = np.frombuffer(raw_carry[:usable], dtype="<f4").copy()
                                del raw_carry[:usable]
                                enqueue_pcm_samples(samples, wait_for_space=True)
                        if raw_carry:
                            audio_thread_error[:] = [
                                "The DRM encoder ended in the middle of a Float32 sample."
                            ]
                        enqueue_pcm_samples([], wait_for_space=True, flush=True)
                    except Exception as exc:
                        audio_thread_error[:] = [_safe_error(exc)]
                    finally:
                        return_code = process.wait()
                        if return_code != 0 and not self._stop_event.is_set():
                            details = "\n".join(drm_stderr_lines[-12:])
                            audio_thread_error[:] = [
                                f"DRM encoder exited with code {return_code}."
                                + (f" {details}" if details else "")
                            ]
                        drm_reader_finished.set()

                def read_drm_stderr() -> None:
                    assert process.stderr is not None
                    for raw_line in process.stderr:
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if line:
                            drm_stderr_lines.append(line[:500])
                            if len(drm_stderr_lines) > 40:
                                del drm_stderr_lines[:-40]

                drm_reader_thread = threading.Thread(
                    target=read_drm_stdout,
                    daemon=True,
                    name="txitpy-drm-audio",
                )
                drm_stderr_thread = threading.Thread(
                    target=read_drm_stderr,
                    daemon=True,
                    name="txitpy-drm-log",
                )
                drm_reader_thread.start()
                drm_stderr_thread.start()

                self.events.put({
                    "kind": "tx_log",
                    "text": "Buffering DRM output before opening the TXITY stream.",
                })
                buffer_deadline = time.monotonic() + 30.0
                while not self._stop_event.is_set():
                    if audio_thread_error:
                        raise RuntimeError(audio_thread_error[0])
                    if audio_queue.qsize() >= DRM_PREROLL_FRAMES or drm_reader_finished.is_set():
                        break
                    if time.monotonic() >= buffer_deadline:
                        raise TimeoutError("The DRM encoder did not produce audio within 30 seconds.")
                    self._stop_event.wait(0.02)

                if self._stop_event.is_set():
                    return
                if audio_queue.empty():
                    details = "\n".join(drm_stderr_lines[-12:])
                    raise RuntimeError(
                        "The DRM encoder did not produce a usable signal."
                        + (f" {details}" if details else "")
                    )
                self.events.put({
                    "kind": "tx_log",
                    "text": f"DRM signal buffer ready: {audio_queue.qsize() * AUDIO_FRAME_INTERVAL_SECONDS:.2f} seconds.",
                })

            if self._stop_event.is_set():
                return

            source_name = "DRM30" if drm_mode else {
                "loopback": "desktop audio",
                "playlist": "playlist audio",
            }.get(audio_source, "microphone audio")
            while not self._stop_event.is_set():
                if not self.client.control_connected():
                    if not control_disconnect_reported:
                        self.events.put({
                            "kind": "tx_log",
                            "text": "TXITY control connection interrupted; holding the audio stream until it reconnects.",
                        })
                        control_disconnect_reported = True
                    control_configured = False
                    self._stop_event.wait(0.25)
                    continue

                if control_disconnect_reported:
                    self.events.put({
                        "kind": "tx_log",
                        "text": "TXITY control connection restored; restoring transmission.",
                    })
                    control_disconnect_reported = False

                current_generation = getattr(self.client, "control_generation", None)
                if (
                    current_generation is not None
                    and last_control_generation is not None
                    and current_generation != last_control_generation
                ):
                    self.events.put({
                        "kind": "tx_log",
                        "text": "TXITY control session reconnected; rebuilding the audio connection.",
                    })
                    control_configured = False
                    if needs_audio:
                        close_audio_connection()

                if needs_audio and not audio_connection_is_open(audio_socket):
                    if audio_socket is not None:
                        self.events.put({
                            "kind": "tx_log",
                            "text": "TXITY audio WebSocket closed; reopening the connection.",
                        })
                    close_audio_connection()
                    audio_socket = open_audio_connection(self.client, "TXITY")
                    if audio_socket is None:
                        break
                    audio_reconnect_attempt = 0
                    control_configured = False

                if not control_configured:
                    try:
                        for label, target_client, endpoint_config in control_endpoints:
                            self.events.put({
                                "kind": "tx_log",
                                "text": (
                                    f"Sending {label} configuration: mode={endpoint_config.get('mode')}, "
                                    f"rfHz={endpoint_config.get('rfHz')}, "
                                    f"allowOverlapVisitors={endpoint_config.get('allowOverlapVisitors')}."
                                ),
                            })
                            target_client.send_control_message(endpoint_config)
                        if self._stop_event.is_set():
                            break
                        for label, target_client, _endpoint_config in control_endpoints:
                            self.events.put({
                                "kind": "tx_log",
                                "text": f"Sending {label} PTT ON request.",
                            })
                            target_client.send_control_message({"type": "ptt", "ptt": True})
                    except Exception as exc:
                        control_configured = False
                        self.events.put({
                            "kind": "tx_log",
                            "text": (
                                f"Could not restore TXITY transmission control ({_safe_error(exc)}); "
                                "retrying."
                            ),
                        })
                        self._stop_event.wait(0.5)
                        continue

                    ptt_sent = True
                    control_configured = True
                    last_control_generation = getattr(
                        self.client, "control_generation", None
                    )
                    with self._lock:
                        self._ptt_sent = True
                        self._ptt_requested_at = time.monotonic()
                        self._active = True
                        self._starting = False
                    if not transmission_announced:
                        self.events.put({
                            "kind": "tx_state",
                            "state": "transmitting",
                            "text": "PTT requested; waiting for TXITY status confirmation.",
                        })
                        transmission_announced = True
                    else:
                        self.events.put({
                            "kind": "tx_log",
                            "text": "TXITY transmission state restored.",
                        })

                if audio_thread_error:
                    raise RuntimeError(audio_thread_error[0])
                if rade_mode and rade_encoder is not None and rade_encoder.error:
                    raise RuntimeError(rade_encoder.error)
                if drm_playlist_server and drm_playlist_server.finished.is_set():
                    if drm_playlist_stop_deadline is None:
                        drm_playlist_stop_deadline = time.monotonic() + 0.6
                    elif (
                        not drm_playlist_stop_requested
                        and time.monotonic() >= drm_playlist_stop_deadline
                        and drm_stop_path is not None
                    ):
                        drm_stop_path.touch(exist_ok=True)
                        drm_playlist_stop_requested = True
                if audio_capture_error:
                    details = audio_capture_error.pop()
                    if rade_mode:
                        raise RuntimeError(details)
                    now = time.monotonic()
                    if now - last_capture_error_at >= 5.0:
                        self.events.put({
                            "kind": "tx_audio_warning",
                            "text": (
                                "Audio input reported an error. MultiV will keep the stream open "
                                f"and send silence until input resumes: {details}"
                            ),
                        })
                        last_capture_error_at = now

                if not needs_audio:
                    self._stop_event.wait(0.2)
                    continue

                using_silence_frame = False
                try:
                    if pending_frame is not None:
                        queued_frame = pending_frame
                    else:
                        queued_frame = audio_queue.get(
                            timeout=(
                                0.05
                                if audio_source == "playlist" or drm_mode
                                else AUDIO_FRAME_INTERVAL_SECONDS
                            )
                        )
                except queue.Empty:
                    if drm_mode:
                        if drm_reader_finished.is_set():
                            if audio_thread_error:
                                raise RuntimeError(audio_thread_error[0])
                            if drm_playlist_server and drm_playlist_server.finished.is_set():
                                playlist_reached_end = True
                                break
                            raise RuntimeError("The DRM encoder stopped before the audio source finished.")
                        continue
                    if audio_source == "playlist" and audio_finished.is_set():
                        if audio_thread_error:
                            raise RuntimeError(audio_thread_error[0])
                        if rade_mode and not rade_finalized and rade_encoder is not None:
                            rade_encoder.finish()
                            rade_finalized = True
                            if rade_encoder.error:
                                raise RuntimeError(rade_encoder.error)
                            enqueue_transport_audio_samples(
                                [],
                                flush=True,
                                playlist_info=rade_playlist_info,
                            )
                            continue
                        playlist_reached_end = True
                        break
                    if audio_source == "playlist":
                        if not playlist_underrun_active:
                            playlist_underruns += 1
                            playlist_underrun_active = True
                        continue
                    if rade_mode:
                        now = time.monotonic()
                        if should_send_live_audio_silence(last_audio_frame_at, now):
                            if rade_encoder is None:
                                raise RuntimeError("The FreeDV RADE encoder is unavailable.")
                            rade_encoder.feed(
                                np.zeros(BATCH_SAMPLES, dtype=np.float32),
                                wait_for_space=False,
                            )
                            last_audio_frame_at = now
                        continue
                    if should_send_live_audio_silence(
                        last_audio_frame_at,
                        time.monotonic(),
                    ):
                        if nfm_modulator is not None:
                            queued_frame = modulate_nfm_samples(
                                np.zeros(BATCH_SAMPLES, dtype=np.float32)
                            ).astype("<f4", copy=False).tobytes()
                        else:
                            queued_frame = bytes(FRAME_BYTES)
                        using_silence_frame = True
                    else:
                        continue

                if audio_source == "playlist":
                    frame, playlist_info = unpack_queued_audio_frame(queued_frame)
                    playlist_underrun_active = False
                    if not drm_mode:
                        if playlist_info != playlist_current_track:
                            playlist_current_track = playlist_info
                            if playlist_info is not None:
                                track_path, track_index, track_count = playlist_info
                                self.events.put({
                                    "kind": "tx_playlist_track",
                                    "path": track_path,
                                    "index": track_index,
                                    "count": track_count,
                                })
                        wait_seconds = playlist_frame_wait_seconds(
                            playlist_next_frame_deadline,
                            time.perf_counter(),
                        )
                        if wait_seconds > 0 and self._stop_event.wait(wait_seconds):
                            break
                        if self._stop_event.is_set():
                            break
                else:
                    frame = queued_frame

                if drm_mode:
                    wait_seconds = playlist_frame_wait_seconds(
                        playlist_next_frame_deadline,
                        time.perf_counter(),
                    )
                    if wait_seconds > 0 and self._stop_event.wait(wait_seconds):
                        break
                    if self._stop_event.is_set():
                        break

                try:
                    audio_socket.send(frame, opcode=websocket.ABNF.OPCODE_BINARY)
                except Exception as exc:
                    if self._stop_event.is_set():
                        break
                    pending_frame = queued_frame
                    audio_reconnect_attempt += 1
                    delay = reconnect_delay(audio_reconnect_attempt)
                    self.events.put({
                        "kind": "tx_log",
                        "text": (
                            f"TXITY audio stream disconnected ({_safe_error(exc)}). "
                            f"Reconnecting in {delay:.0f} seconds."
                        ),
                    })
                    close_audio_connection()
                    control_configured = False
                    self._stop_event.wait(delay)
                    continue
                pending_frame = None
                audio_reconnect_attempt = 0
                audio_frames_sent += 1
                progress_now = time.monotonic()
                if audio_frames_sent == 1:
                    self.events.put({
                        "kind": "tx_log",
                        "text": (
                            f"First FreeDV RADE audio frame sent to TXITY ({len(frame)} bytes)."
                            if rade_mode
                            else f"First audio frame sent to TXITY ({len(frame)} bytes)."
                        ),
                    })
                    last_audio_progress_at = progress_now
                elif progress_now - last_audio_progress_at >= 30.0:
                    audio_duration = audio_frames_sent * AUDIO_FRAME_INTERVAL_SECONDS
                    self.events.put({
                        "kind": "tx_log",
                        "text": (
                            f"Audio stream active: {audio_frames_sent} frames sent "
                            f"({audio_duration:.1f} seconds of audio)."
                        ),
                    })
                    last_audio_progress_at = progress_now
                if audio_source in {"mic", "loopback"} and not using_silence_frame:
                    last_audio_frame_at = time.monotonic()
                if drm_mode:
                    playlist_next_frame_deadline = next_audio_frame_deadline(
                        playlist_next_frame_deadline,
                        time.perf_counter(),
                        catch_up_after_delay=True,
                    )
                elif audio_source == "playlist":
                    playlist_next_frame_deadline = next_audio_frame_deadline(
                        playlist_next_frame_deadline,
                        time.perf_counter(),
                    )

        except Exception as exc:
            if not self._stop_event.is_set():
                self.events.put({
                    "kind": "tx_error",
                    "text": _safe_error(exc),
                    "traceback": _safe_traceback(exc),
                })
        finally:
            playlist_completed = playlist_reached_end and not audio_thread_error
            self._stop_event.set()

            if rade_encoder is not None:
                if input_stream is not None:
                    try:
                        input_stream.stop()
                    except Exception:
                        pass
                    try:
                        input_stream.close()
                    except Exception:
                        pass
                    input_stream = None

                if loopback_stream is not None:
                    try:
                        if loopback_stream.is_active():
                            loopback_stream.stop_stream()
                    except Exception:
                        pass
                    try:
                        loopback_stream.close()
                    except Exception:
                        pass
                    loopback_stream = None

                if loopback_manager is not None:
                    try:
                        loopback_manager.terminate()
                    except Exception:
                        pass
                    loopback_manager = None

                if playlist_thread is not None and playlist_thread is not threading.current_thread():
                    playlist_thread.join(timeout=2.0)

                try:
                    rade_encoder.finish()
                    if rade_encoder.error:
                        self.events.put({
                            "kind": "tx_log",
                            "text": f"FreeDV RADE encoder shutdown: {rade_encoder.error[:700]}",
                        })
                    enqueue_transport_audio_samples(
                        [],
                        flush=True,
                        playlist_info=rade_playlist_info,
                    )
                except Exception as exc:
                    self.events.put({
                        "kind": "tx_log",
                        "text": f"FreeDV RADE final-frame handling failed: {_safe_error(exc)}",
                    })

                if ptt_sent and audio_socket_is_available_for_drain(audio_socket):
                    pending_items = []
                    if pending_frame is not None:
                        pending_items.append(pending_frame)
                        pending_frame = None
                    while True:
                        try:
                            pending_items.append(audio_queue.get_nowait())
                        except queue.Empty:
                            break

                    for item in pending_items:
                        frame = item[0] if isinstance(item, tuple) else item
                        try:
                            audio_socket.send(
                                frame,
                                opcode=websocket.ABNF.OPCODE_BINARY,
                            )
                        except Exception as exc:
                            self.events.put({
                                "kind": "tx_log",
                                "text": f"Could not flush the final RADE audio frame: {_safe_error(exc)}",
                            })
                            break
                        time.sleep(AUDIO_FRAME_INTERVAL_SECONDS)

            if ptt_sent:
                for label, target_client, _endpoint_config in control_endpoints:
                    try:
                        target_client.send_control_message({"type": "ptt", "ptt": False})
                        self.events.put({
                            "kind": "tx_log",
                            "text": f"Sent {label} PTT OFF request.",
                        })
                    except Exception as exc:
                        self.events.put({
                            "kind": "tx_log",
                            "text": f"Could not send {label} PTT OFF request: {_safe_error(exc)}",
                        })

            if drm_stop_path is not None:
                try:
                    drm_stop_path.touch(exist_ok=True)
                except OSError:
                    pass
            if drm_process is not None:
                try:
                    drm_process.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    drm_process.terminate()
                    try:
                        drm_process.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        drm_process.kill()
                        drm_process.wait(timeout=2.0)
            if drm_reader_thread is not None and drm_reader_thread is not threading.current_thread():
                drm_reader_thread.join(timeout=3.0)
            if drm_stderr_thread is not None and drm_stderr_thread is not threading.current_thread():
                drm_stderr_thread.join(timeout=2.0)
            if drm_playlist_server is not None:
                try:
                    drm_playlist_server.close()
                except Exception:
                    pass
            if drm_temp_dir is not None:
                try:
                    drm_temp_dir.cleanup()
                except Exception:
                    pass
            if playlist_thread is not None and playlist_thread is not threading.current_thread():
                playlist_thread.join(timeout=2.0)

            if input_stream is not None:
                try:
                    input_stream.stop()
                except Exception:
                    pass
                try:
                    input_stream.close()
                except Exception:
                    pass

            if loopback_stream is not None:
                try:
                    if loopback_stream.is_active():
                        loopback_stream.stop_stream()
                except Exception:
                    pass
                try:
                    loopback_stream.close()
                except Exception:
                    pass

            if loopback_manager is not None:
                try:
                    loopback_manager.terminate()
                except Exception:
                    pass

            if icecast_broadcaster is not None:
                try:
                    icecast_broadcaster.stop()
                except Exception as exc:
                    self.events.put({
                        "kind": "tx_icecast_state",
                        "name": "Icecast",
                        "state": "Shutdown error",
                        "text": f"Could not stop Icecast outputs cleanly: {_safe_error(exc)}",
                    })

            close_audio_connection()

            if audio_timer_resolution is not None:
                try:
                    audio_timer_resolution.timeEndPeriod(1)
                except Exception:
                    pass

            end_windows_system_awake(system_awake_handle)

            if playlist_completed:
                self.events.put({"kind": "tx_playlist_finished"})

            with self._lock:
                self._active = False
                self._starting = False
                self._ptt_sent = False
                self._ptt_requested_at = None
                self._thread = None

            self.events.put({
                "kind": "tx_state",
                "state": "stopped",
                "text": "Playlist finished." if playlist_completed else "Transmission stopped.",
            })
