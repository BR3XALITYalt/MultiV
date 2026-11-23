"""MultiV desktop client for TXITY's station directory, status feed, and virtual transmitter.

Discord sign-in uses TXITY's official OAuth route and a one-use loopback callback.
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any

from app_assets import asset_path
from audio_playlist import (
    build_playlist_snapshot,
    normalize_audio_sources,
)
from discord_auth import (
    DiscordAuthManager,
    clean_discord_user,
    clear_auth_token,
    format_exception_details,
    is_auth_token,
    safe_error_text,
    write_diagnostic,
)
from drm_support import (
    DRM_LANGUAGES,
    DRM_PROGRAMME_TYPES,
    DRM_AUDIO_CODECS,
    DRM_MSC_MODES,
    DRM_QUALITY_PROFILES,
    drm_quality_profile_settings,
    drm_setup_status,
    prepare_drm_encoder,
)
from nfm_modulator import NFM_PROFILES, NFM_DEFAULT_PROFILE
from transmitter import (
    TxityTransmitter,
    apply_nfm_transport,
    apply_rade_transport,
    build_frequency_candidates,
    build_transmit_config,
    find_frequency_overlaps,
    list_audio_inputs,
    list_loopback_devices,
    parse_rf_frequency,
)
from icecast_output import (
    ICECAST_BITRATES,
    ICECAST_FORMATS,
    available_icecast_formats,
    normalize_icecast_destination,
)
from web_gui import TxityWebGUI, create_password_record
from station_directory import (
    Station,
    describe_media_url,
    fetch_station_image,
    format_frequency,
)
from txity_client import TxityClient
from build_info import APP_COMMIT, APP_VERSION
from update_manager import check_for_update, install_update, is_packaged

BASE_URL = "https://txity.radiodiscussion.org"


def tx_settings_file() -> Path:
    """Return the per-user settings file path without storing account credentials."""
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / "TXITPY" / "transmit-settings.json"


def load_tx_settings() -> dict[str, Any]:
    """Load local station and transmit preferences, ignoring invalid files safely."""
    path = tx_settings_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        return {}
    return data if isinstance(data, dict) else {}


def _system_theme_mode() -> str:
    """Read the Windows application theme preference when available."""
    if sys.platform == "win32":
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            )
            value, _kind = winreg.QueryValueEx(key, "AppsUseLightTheme")
            winreg.CloseKey(key)
            return "light" if value else "dark"
        except (OSError, ImportError):
            pass
    return "light"


def build_app() -> None:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    from sun_valley_theme import apply_tk_theme
    client = TxityClient()
    auth_manager = DiscordAuthManager(client.events)
    transmitter = TxityTransmitter(client, client.events)

    theme_mode = "dark"

    root = tk.Tk()
    try:
        root._multiv_icon = tk.PhotoImage(file=str(asset_path("logo.png")))
        root.iconphoto(True, root._multiv_icon)
    except (tk.TclError, OSError):
        pass
    palette = apply_tk_theme(root, theme_mode)
    root.title("MultiV")
    root.geometry("1180x790")
    root.minsize(900, 650)
    root.configure(background=palette["background"])

    update_status_var = tk.StringVar(
        root,
        value=(
            f"Version {APP_VERSION}"
            if is_packaged()
            else "Source checkout. Automatic updates are available in packaged builds."
        ),
    )
    update_state: dict[str, Any] = {"info": None, "checking": False}
    update_check_button: Any = None
    update_install_button: Any = None

    tray_icon: Any = None
    tray_setup_ready = threading.Event()

    saved_tx_settings = load_tx_settings()

    def saved_text(key: str, default: str) -> str:
        value = saved_tx_settings.get(key, default)
        return str(value) if value is not None else default

    def saved_bool(key: str, default: bool) -> bool:
        value = saved_tx_settings.get(key, default)
        return value if isinstance(value, bool) else default

    fade_shape_names = {
        "0": "Smooth QSB dip",
        "1": "Linear triangle dip",
        "2": "Deep notch fade",
        "3": "Fast flutter / multipath",
        "4": "Rolling multi-rate QSB",
        "5": "Random QSB",
    }
    saved_fade_shape_id = saved_text("fade_shape", "0").split("-", 1)[0].strip()
    if saved_fade_shape_id not in fade_shape_names:
        saved_fade_shape_id = "0"
    fade_shape_saved_value = f"{saved_fade_shape_id} - {fade_shape_names[saved_fade_shape_id]}"

    # Keep Tk variables with the root; navigation and theme changes rebuild page widgets.
    connection_var = tk.StringVar(root, "Disconnected")
    account_var = tk.StringVar(root, "Not signed in to Discord")
    auth_status_var = tk.StringVar(root, "Checking TXITY server configuration...")
    station_status_var = tk.StringVar(root, "Waiting for TXITY station data.")
    station_count_var = tk.StringVar(root, "0 stations")

    tx_station_name_var = tk.StringVar(root, saved_text("station_name", ""))
    tx_description_var = tk.StringVar(root, saved_text("description", ""))
    tx_frequency_var = tk.StringVar(root, saved_text("frequency", "7.250 MHz"))
    saved_frequency_fallbacks = saved_tx_settings.get("frequency_fallbacks", [])
    if not isinstance(saved_frequency_fallbacks, list):
        saved_frequency_fallbacks = []
    tx_frequency_fallback_vars = [
        tk.StringVar(root, str(value))
        for value in saved_frequency_fallbacks
        if str(value).strip()
    ][:7]
    tx_frequency_check_var = tk.StringVar(root, "Waiting for TXITY station data.")
    saved_mode = saved_text("mode", "AM").strip().upper()
    if saved_mode == "DRM [EXPERIMENTAL]":
        saved_mode = "DRM"
    elif saved_mode == "AM (DEV)":
        saved_mode = "AM"
    tx_mode_var = tk.StringVar(root, saved_mode)
    tx_mode_display_var = tk.StringVar(
        root,
        "DRM [EXPERIMENTAL]" if saved_mode == "DRM" else saved_mode,
    )
    tx_mode_help_var = tk.StringVar(root, "AM uses a carrier with upper and lower sidebands.")
    tx_drm_service_label_var = tk.StringVar(root, saved_text("drm_service_label", ""))
    tx_drm_service_id_var = tk.StringVar(root, saved_text("drm_service_id", "0xB30001"))
    tx_drm_language_var = tk.StringVar(root, saved_text("drm_language", "English"))
    tx_drm_programme_type_var = tk.StringVar(root, saved_text("drm_programme_type", "No programme type"))
    tx_drm_iso_language_var = tk.StringVar(root, saved_text("drm_iso_language", ""))
    tx_drm_iso_country_var = tk.StringVar(root, saved_text("drm_iso_country", ""))
    saved_drm_quality_profile = saved_text(
        "drm_quality_profile",
        DRM_QUALITY_PROFILES[0],
    )
    if saved_drm_quality_profile not in DRM_QUALITY_PROFILES:
        saved_drm_quality_profile = DRM_QUALITY_PROFILES[0]
    tx_drm_quality_profile_var = tk.StringVar(root, saved_drm_quality_profile)
    tx_drm_audio_codec_var = tk.StringVar(root, saved_text("drm_audio_codec", "xHE-AAC"))
    tx_drm_msc_mode_var = tk.StringVar(root, saved_text("drm_msc_mode", "16-QAM"))
    tx_drm_stereo_var = tk.BooleanVar(root, saved_bool("drm_stereo", False))
    tx_drm_interleaving_var = tk.StringVar(root, saved_text("drm_interleaving", "Long (2 s)"))
    drm_setup_state: dict[str, Any] = {
        "running": False,
        "previous_mode": (
            tx_mode_var.get().strip().upper()
            if tx_mode_var.get().strip().upper() not in {"", "DRM"}
            else "AM"
        ),
    }
    drm_setup_events: queue.Queue[tuple[str, str]] = queue.Queue()
    tx_power_var = tk.StringVar(root, saved_text("station_dbm", "50"))
    tx_power_readout_var = tk.StringVar(root, "50 dBm")
    tx_audio_source_var = tk.StringVar(root, saved_text("audio_source", "Microphone / line input"))
    tx_audio_device_var = tk.StringVar(root, saved_text("audio_device", "System default"))
    tx_audio_device_label_var = tk.StringVar(root, "Input device")
    tx_audio_input_var = tx_audio_device_var
    saved_playlist_files = saved_tx_settings.get("playlist_files", [])
    saved_playlist_sources = saved_tx_settings.get("playlist_sources")
    if not isinstance(saved_playlist_sources, list):
        saved_playlist_sources = saved_playlist_files
    tx_playlist_sources = normalize_audio_sources(
        saved_playlist_sources if isinstance(saved_playlist_sources, list) else []
    )
    saved_playlist_exclusions = saved_tx_settings.get("playlist_excluded_files", [])
    tx_playlist_excluded_files = {
        path.casefold()
        for path in normalize_audio_sources(
            saved_playlist_exclusions if isinstance(saved_playlist_exclusions, list) else []
        )
    }
    tx_playlist_files, tx_playlist_signature = build_playlist_snapshot(
        tx_playlist_sources,
        tx_playlist_excluded_files,
    )
    playlist_watch_lock = threading.Lock()
    playlist_watch_revision = 0
    playlist_watch_signature = tx_playlist_signature
    playlist_watch_stop = threading.Event()
    playlist_ui_refresh_callback: Any = None
    tx_playlist_shuffle_var = tk.BooleanVar(root, saved_bool("playlist_shuffle", False))
    tx_playlist_loop_var = tk.BooleanVar(root, saved_bool("playlist_loop", False))
    tx_playlist_status_var = tk.StringVar(
        root,
        (
            f"{len(tx_playlist_files)} track{'s' if len(tx_playlist_files) != 1 else ''} loaded."
            if tx_playlist_files else "No playlist loaded."
        ),
    )
    tx_cw_text_var = tk.StringVar(root, saved_text("cw_text", "CQ CQ DE MULTIV"))
    tx_cw_wpm_var = tk.StringVar(root, saved_text("cw_wpm", "18"))
    tx_cw_loop_var = tk.BooleanVar(root, saved_bool("cw_loop", True))
    tx_allow_overlap_var = tk.BooleanVar(root, saved_bool("allow_overlap", False))
    tx_logo_url_var = tk.StringVar(root, saved_text("station_logo_url", ""))
    tx_logo_upload_status_var = tk.StringVar(root, "")
    station_logo_upload_button: Any = None
    tx_details_var = tk.StringVar(root, saved_text("station_details", ""))
    tx_page_layout_var = tk.StringVar(root, saved_text("station_page_layout", ""))

    web_gui_enabled_var = tk.BooleanVar(root, saved_bool("web_gui_enabled", False))
    web_gui_password_var = tk.StringVar(root, "")
    web_gui_status_var = tk.StringVar(root, "Web GUI is disabled.")
    web_gui_password_salt = saved_text("web_gui_password_salt", "")
    web_gui_password_hash = saved_text("web_gui_password_hash", "")
    web_gui_server: TxityWebGUI | None = None
    web_gui_commands: queue.Queue[dict[str, Any]] = queue.Queue()
    web_gui_state_lock = threading.Lock()
    web_gui_state: dict[str, Any] = {}

    tx_fade_enabled_var = tk.BooleanVar(root, saved_bool("fade_enabled", False))
    tx_fade_seconds_var = tk.StringVar(root, saved_text("fade_seconds", "15"))
    tx_fade_min_dbm_var = tk.StringVar(root, saved_text("fade_min_dbm", "25"))
    tx_fade_shape_var = tk.StringVar(root, fade_shape_saved_value)
    tx_agc_enabled_var = tk.BooleanVar(root, saved_bool("agc_enabled", True))
    tx_agc_target_var = tk.StringVar(root, saved_text("agc_target", "0.19"))
    tx_mic_gain_var = tk.StringVar(root, saved_text("mic_gain_db", "0"))
    tx_bass_var = tk.StringVar(root, saved_text("bass_db", "0"))
    tx_treble_var = tk.StringVar(root, saved_text("treble_db", "0"))
    tx_presence_var = tk.StringVar(root, saved_text("presence_db", "0"))
    tx_drive_var = tk.StringVar(root, saved_text("drive", "0.015"))
    tx_nfm_transport_bandwidth_var = tk.StringVar(
        root, saved_text("nfm_transport_bandwidth_hz", "12000")
    )
    saved_nfm_profile = saved_text("nfm_profile", NFM_DEFAULT_PROFILE)
    if saved_nfm_profile not in NFM_PROFILES:
        saved_nfm_profile = NFM_DEFAULT_PROFILE
    tx_nfm_profile_var = tk.StringVar(root, saved_nfm_profile)
    tx_ssb_bandwidth_var = tk.StringVar(root, saved_text("ssb_bandwidth_hz", "10000"))
    tx_ssb_low_cut_var = tk.StringVar(root, saved_text("ssb_low_cut_hz", "325"))
    tx_ssb_high_cut_var = tk.StringVar(root, saved_text("ssb_high_cut_hz", "0"))
    tx_am_bandwidth_var = tk.StringVar(root, saved_text("am_bandwidth_hz", "12000"))
    tx_am_pre_emphasis_var = tk.BooleanVar(root, saved_bool("am_pre_emphasis", False))
    saved_icecast_destinations = saved_tx_settings.get("icecast_destinations", [])
    if not isinstance(saved_icecast_destinations, list):
        saved_icecast_destinations = []
    icecast_destinations: list[dict[str, Any]] = []
    for saved_destination in saved_icecast_destinations[:8]:
        if not isinstance(saved_destination, dict):
            continue
        icecast_destinations.append({
            "url": str(saved_destination.get("url") or "")[:2_048],
            "username": str(saved_destination.get("username") or "source")[:128],
            "password": str(saved_destination.get("password") or ""),
            "enabled": bool(saved_destination.get("enabled", True)),
            "format": str(saved_destination.get("format") or "mp3"),
            "bitrate": str(saved_destination.get("bitrate") or "128"),
        })
    tx_icecast_bitrate_var = tk.StringVar(
        root,
        saved_text("icecast_bitrate_kbps", "96"),
    )
    tx_icecast_status_var = tk.StringVar(root, "No Icecast destinations configured.")
    tx_icecast_states: dict[str, str] = {}
    txity_output_enabled_var = tk.BooleanVar(
        root, saved_bool("txity_output_enabled", True)
    )
    icecast_output_enabled_var = tk.BooleanVar(
        root, saved_bool("icecast_output_enabled", True)
    )
    tx_icecast_server_enabled_var = tk.BooleanVar(
        root, saved_bool("icecast_server_enabled", False)
    )
    tx_icecast_server_format_var = tk.StringVar(
        root, saved_text("icecast_server_format", "ogg_opus")
    )
    saved_output_order = saved_tx_settings.get("output_order", ["txity", "icecast"])
    if not isinstance(saved_output_order, list):
        saved_output_order = []
    output_order = [
        name for name in saved_output_order
        if name in {"txity", "icecast"}
    ]
    output_order = list(dict.fromkeys(output_order))
    for provider_name in ("txity", "icecast"):
        if provider_name not in output_order:
            output_order.append(provider_name)
    tx_icecast_server_port_var = tk.StringVar(
        root, saved_text("icecast_server_port", "8000")
    )
    tx_icecast_server_mount_var = tk.StringVar(
        root, saved_text("icecast_server_mount", "/live")
    )
    tx_icecast_server_password_var = tk.StringVar(
        root, saved_text("icecast_server_listener_password", "")
    )
    tx_icecast_server_max_listeners_var = tk.StringVar(
        root, saved_text("icecast_server_max_listeners", "25")
    )

    tx_status_var = tk.StringVar(root, "Not transmitting")
    tx_audio_device_ids: dict[str, int | None] = {"System default": None}
    tx_profile_initialized = bool(saved_text("station_name", "").strip())

    settings_save_after_id: str | None = None

    def tx_settings_snapshot() -> dict[str, Any]:
        return {
            "station_name": tx_station_name_var.get(),
            "description": tx_description_var.get(),
            "frequency": tx_frequency_var.get(),
            "frequency_fallbacks": [
                variable.get().strip()
                for variable in tx_frequency_fallback_vars
                if variable.get().strip()
            ],
            "mode": tx_mode_var.get(),
            "drm_service_label": tx_drm_service_label_var.get(),
            "drm_service_id": tx_drm_service_id_var.get(),
            "drm_language": tx_drm_language_var.get(),
            "drm_programme_type": tx_drm_programme_type_var.get(),
            "drm_iso_language": tx_drm_iso_language_var.get(),
            "drm_iso_country": tx_drm_iso_country_var.get(),
            "drm_quality_profile": tx_drm_quality_profile_var.get(),
            "drm_audio_codec": tx_drm_audio_codec_var.get(),
            "drm_msc_mode": tx_drm_msc_mode_var.get(),
            "drm_stereo": tx_drm_stereo_var.get(),
            "drm_interleaving": tx_drm_interleaving_var.get(),
            "station_dbm": tx_power_var.get(),
            "audio_source": tx_audio_source_var.get(),
            "audio_device": tx_audio_device_var.get(),
            "icecast_bitrate_kbps": tx_icecast_bitrate_var.get(),
            "icecast_destinations": [dict(item) for item in icecast_destinations],
            "txity_output_enabled": txity_output_enabled_var.get(),
            "icecast_output_enabled": icecast_output_enabled_var.get(),
            "output_order": list(output_order),
            "icecast_server_enabled": tx_icecast_server_enabled_var.get(),
            "icecast_server_format": tx_icecast_server_format_var.get(),
            "icecast_server_port": tx_icecast_server_port_var.get(),
            "icecast_server_mount": tx_icecast_server_mount_var.get(),
            "icecast_server_listener_password": tx_icecast_server_password_var.get(),
            "icecast_server_max_listeners": tx_icecast_server_max_listeners_var.get(),
            "playlist_files": list(tx_playlist_files),
            "playlist_sources": list(tx_playlist_sources),
            "playlist_excluded_files": sorted(tx_playlist_excluded_files),
            "playlist_shuffle": tx_playlist_shuffle_var.get(),
            "playlist_loop": tx_playlist_loop_var.get(),
            "cw_text": tx_cw_text_var.get(),
            "cw_wpm": tx_cw_wpm_var.get(),
            "cw_loop": tx_cw_loop_var.get(),
            "allow_overlap": tx_allow_overlap_var.get(),
            "station_logo_url": tx_logo_url_var.get(),
            "station_details": tx_details_var.get(),
            "station_page_layout": tx_page_layout_var.get(),
            "web_gui_enabled": web_gui_enabled_var.get(),
            "web_gui_password_salt": web_gui_password_salt,
            "web_gui_password_hash": web_gui_password_hash,
            "fade_enabled": tx_fade_enabled_var.get(),
            "fade_seconds": tx_fade_seconds_var.get(),
            "fade_min_dbm": tx_fade_min_dbm_var.get(),
            "fade_shape": tx_fade_shape_var.get(),
            "agc_enabled": tx_agc_enabled_var.get(),
            "agc_target": tx_agc_target_var.get(),
            "mic_gain_db": tx_mic_gain_var.get(),
            "bass_db": tx_bass_var.get(),
            "treble_db": tx_treble_var.get(),
            "presence_db": tx_presence_var.get(),
            "drive": tx_drive_var.get(),
            "nfm_transport_bandwidth_hz": tx_nfm_transport_bandwidth_var.get(),
            "nfm_profile": tx_nfm_profile_var.get(),
            "ssb_bandwidth_hz": tx_ssb_bandwidth_var.get(),
            "ssb_low_cut_hz": tx_ssb_low_cut_var.get(),
            "ssb_high_cut_hz": tx_ssb_high_cut_var.get(),
            "am_bandwidth_hz": tx_am_bandwidth_var.get(),
            "am_pre_emphasis": tx_am_pre_emphasis_var.get(),
        }

    def save_tx_settings_now() -> None:
        nonlocal settings_save_after_id
        if settings_save_after_id is not None:
            try:
                root.after_cancel(settings_save_after_id)
            except tk.TclError:
                pass
            settings_save_after_id = None
        path = tx_settings_file()
        temporary = path.with_suffix(path.suffix + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(tx_settings_snapshot(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            if os.name != "nt":
                os.chmod(temporary, 0o600)
            temporary.replace(path)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def schedule_tx_settings_save(*_args) -> None:
        nonlocal settings_save_after_id
        if settings_save_after_id is not None:
            try:
                root.after_cancel(settings_save_after_id)
            except tk.TclError:
                pass
        settings_save_after_id = root.after(450, save_tx_settings_now)

    def refresh_playlist_from_sources(*, save: bool = True) -> tuple[int, int]:
        nonlocal playlist_watch_revision, playlist_watch_signature
        with playlist_watch_lock:
            sources = list(tx_playlist_sources)
            excluded = set(tx_playlist_excluded_files)
        refreshed_files, signature = build_playlist_snapshot(sources, excluded)
        previous = {path.casefold() for path in tx_playlist_files}
        current = {path.casefold() for path in refreshed_files}
        added = len(current - previous)
        removed = len(previous - current)
        tx_playlist_files[:] = refreshed_files
        with playlist_watch_lock:
            playlist_watch_revision += 1
            playlist_watch_signature = signature
        if playlist_ui_refresh_callback is not None:
            try:
                playlist_ui_refresh_callback()
            except tk.TclError:
                pass
        if save:
            schedule_tx_settings_save()
        if transmitter.is_busy and tx_audio_source_var.get() == "Audio playlist":
            schedule_live_reconfigure()
        return added, removed

    tx_setting_variables = (
        tx_station_name_var, tx_description_var, tx_frequency_var, tx_mode_var,
        tx_drm_service_label_var, tx_drm_service_id_var, tx_drm_language_var,
        tx_drm_programme_type_var, tx_drm_iso_language_var, tx_drm_iso_country_var,
        tx_drm_quality_profile_var, tx_drm_audio_codec_var, tx_drm_stereo_var,
        tx_drm_interleaving_var,
        tx_power_var, tx_audio_source_var, tx_audio_device_var,
        tx_playlist_shuffle_var, tx_playlist_loop_var, tx_cw_text_var,
        tx_cw_wpm_var, tx_cw_loop_var, tx_allow_overlap_var, tx_logo_url_var,
        tx_details_var, tx_page_layout_var, web_gui_enabled_var, tx_fade_enabled_var,
        tx_fade_seconds_var, tx_fade_min_dbm_var, tx_fade_shape_var,
        tx_agc_enabled_var, tx_agc_target_var, tx_mic_gain_var, tx_bass_var,
        tx_treble_var, tx_presence_var, tx_drive_var, tx_nfm_transport_bandwidth_var,
        tx_nfm_profile_var,
        tx_ssb_bandwidth_var,
        tx_ssb_low_cut_var, tx_ssb_high_cut_var, tx_am_bandwidth_var,
        tx_am_pre_emphasis_var, tx_icecast_bitrate_var,
        tx_icecast_server_enabled_var, tx_icecast_server_port_var,
        tx_icecast_server_mount_var, tx_icecast_server_password_var,
        tx_icecast_server_max_listeners_var,
    )
    for setting_variable in tx_setting_variables:
        setting_variable.trace_add("write", schedule_tx_settings_save)
    for fallback_variable in tx_frequency_fallback_vars:
        fallback_variable.trace_add("write", schedule_tx_settings_save)

    def update_tx_power_readout(*_args) -> None:
        try:
            value = float(tx_power_var.get())
            tx_power_readout_var.set(f"{value:.0f} dBm")
        except (TypeError, ValueError):
            tx_power_readout_var.set("Invalid power")

    tx_power_var.trace_add("write", update_tx_power_readout)
    update_tx_power_readout()
    tx_transmit_button: tk.Button | None = None

    discord_authenticated = False
    auth_restore_started = False
    auth_enabled = False
    users_listing_received = False
    station_rows: list[dict[str, Any]] = []
    server_band_min_hz: int | None = None
    server_band_max_hz: int | None = None
    tx_negative_status_reason: str | None = None
    tx_negative_status_count = 0
    tx_ptt_confirmed = False
    tx_last_status_signature: tuple[Any, ...] | None = None
    tx_terminal_server_message: str | None = None
    frequency_retry_context: dict[str, Any] | None = None
    station_search_var = tk.StringVar(root, "")
    station_cards_frame: ttk.Frame | None = None
    station_directory_window: tk.Toplevel | None = None
    selected_station_id = ""
    image_cache: dict[tuple[str, int], Any] = {}
    image_loading: set[str] = set()
    image_labels: dict[str, list[tuple[ttk.Label, int, str]]] = {}
    current_page = "transmit"
    shell_frame: ttk.Frame | None = None
    content_frame: ttk.Frame | None = None
    nav_frame: ttk.Frame | None = None
    active_scroll_canvas: tk.Canvas | None = None
    SCROLL_PIXELS_PER_NOTCH = 96
    SCROLL_UNIT_PIXELS = 16
    pending_scroll_pixels = 0.0
    scroll_flush_scheduled = False

    def flush_pending_scroll() -> None:
        nonlocal pending_scroll_pixels, scroll_flush_scheduled
        scroll_flush_scheduled = False
        canvas = active_scroll_canvas
        if canvas is None or abs(pending_scroll_pixels) < SCROLL_UNIT_PIXELS / 2:
            return
        try:
            if not canvas.winfo_exists() or not canvas.bbox("all"):
                pending_scroll_pixels = 0.0
                return
            units = int(round(pending_scroll_pixels / SCROLL_UNIT_PIXELS))
            if units:
                pending_scroll_pixels -= units * SCROLL_UNIT_PIXELS
                canvas.yview_scroll(units, "units")
        except tk.TclError:
            pending_scroll_pixels = 0.0

    def scroll_active_canvas(event, direction: int | None = None):
        nonlocal pending_scroll_pixels, scroll_flush_scheduled
        canvas = active_scroll_canvas
        if canvas is None:
            return

        try:
            if not canvas.winfo_exists():
                return
            left = canvas.winfo_rootx()
            top = canvas.winfo_rooty()
            right = left + canvas.winfo_width()
            bottom = top + canvas.winfo_height()
            if not (left <= event.x_root < right and top <= event.y_root < bottom):
                return
            if not canvas.bbox("all"):
                return
        except tk.TclError:
            return

        if direction is not None:
            scroll_delta = direction * SCROLL_PIXELS_PER_NOTCH
        else:
            delta = getattr(event, "delta", 0)
            if not delta:
                return
            scroll_delta = -(float(delta) / 120.0) * SCROLL_PIXELS_PER_NOTCH

        pending_scroll_pixels += scroll_delta
        if not scroll_flush_scheduled:
            scroll_flush_scheduled = True
            root.after_idle(flush_pending_scroll)
        return "break"

    root.bind_all("<MouseWheel>", scroll_active_canvas, add="+")
    root.bind_all("<Button-4>", lambda event: scroll_active_canvas(event, -1), add="+")
    root.bind_all("<Button-5>", lambda event: scroll_active_canvas(event, 1), add="+")

    def apply_current_theme() -> None:
        nonlocal palette
        palette = apply_tk_theme(root, theme_mode)
        root.configure(background=palette["background"])

    def make_label(
        parent: tk.Misc,
        text: str | None = None,
        *,
        variable: tk.StringVar | None = None,
        font: tuple[str, int, str] = ("Segoe UI", 10, "normal"),
        color: str | None = None,
        wraplength: int = 0,
        justify: str = "left",
        anchor: str = "w",
    ) -> ttk.Label:
        options: dict[str, Any] = {
            "font": font,
            "justify": justify,
            "anchor": anchor,
        }
        if color is not None:
            options["foreground"] = color
        if variable is not None:
            options["textvariable"] = variable
        else:
            options["text"] = text or ""
        if wraplength:
            options["wraplength"] = wraplength
        return ttk.Label(parent, **options)

    def make_button(
        parent: tk.Misc,
        text: str,
        command: Any,
        *,
        kind: str = "primary",
        padx: int = 16,
        pady: int = 9,
    ) -> ttk.Button:
        return ttk.Button(
            parent,
            text=text,
            command=command,
            style="Accent.TButton" if kind == "primary" else "TButton",
            padding=(padx, pady),
            cursor="hand2",
        )

    def set_station_logo_upload_button_enabled(enabled: bool) -> None:
        nonlocal station_logo_upload_button
        if station_logo_upload_button is None:
            return
        try:
            if station_logo_upload_button.winfo_exists():
                station_logo_upload_button.configure(
                    state="normal" if enabled else "disabled"
                )
            else:
                station_logo_upload_button = None
        except tk.TclError:
            station_logo_upload_button = None

    def make_card(
        parent: tk.Misc,
        *,
        padding: int = 16,
    ) -> ttk.Frame:
        return ttk.Frame(parent, padding=padding)

    def request_station_image(url: str) -> None:
        if not url:
            return
        display_url = describe_media_url(url)
        if url in image_loading:
            return

        image_loading.add(url)

        def worker() -> None:
            try:
                data = fetch_station_image(url)
                client.events.put({
                    "kind": "station_image",
                    "url": url,
                    "data": data,
                    "byte_count": len(data),
                })
            except Exception as exc:
                client.events.put({
                    "kind": "station_image_error",
                    "url": url,
                    "text": safe_error_text(str(exc), max_chars=500),
                })

        threading.Thread(
            target=worker,
            daemon=True,
            name="txity-station-image",
        ).start()

    def make_image_label(
        parent: tk.Misc,
        url: str,
        *,
        size: int,
        placeholder: str = "TX",
        role: str = "station image",
    ) -> ttk.Label:
        label = ttk.Label(
            parent,
            text=placeholder,
            font=("Segoe UI", max(16, size // 5), "bold"),
            foreground=palette["mutedForeground"],
            width=max(5, size // 9),
            anchor="center",
            justify="center",
        )
        if not url:
            return label

        key = (url, size)
        photo = image_cache.get(key)
        if photo is not None:
            label.configure(image=photo, text="", width=size, height=size)
            label.image = photo
            return label

        image_labels.setdefault(url, []).append((label, size, role))
        request_station_image(url)
        return label

    def apply_station_image(url: str, data: bytes) -> None:
        image_loading.discard(url)
        waiting = image_labels.pop(url, [])
        display_url = describe_media_url(url)
        try:
            from io import BytesIO
            from PIL import Image, ImageOps, ImageTk

            source = Image.open(BytesIO(data))
            source.load()
            source = source.convert("RGBA")
        except Exception as exc:
            return

        applied = 0
        for label, size, role in waiting:
            try:
                if not label.winfo_exists():
                    continue
                image = ImageOps.fit(
                    source,
                    (size, size),
                    method=Image.Resampling.LANCZOS,
                    centering=(0.5, 0.5),
                )
                photo = ImageTk.PhotoImage(image)
                image_cache[(url, size)] = photo
                label.configure(
                    image=photo,
                    text="",
                    width=size,
                    height=size,
                )
                label.image = photo
                applied += 1
            except Exception as exc:
                pass

        if waiting and not applied:
            pass

    def bind_station_card(
        widget: tk.Widget,
        station_id: str,
        *,
        card_root: bool = True,
    ) -> None:
        widget.configure(cursor="hand2")
        widget.bind(
            "<Button-1>",
            lambda _event, target=station_id: open_station(target),
            add="+",
        )
        if card_root:
            widget.configure(
                takefocus=True,
                highlightthickness=2,
                highlightbackground=palette["outline"],
                highlightcolor=palette["accent"],
            )

            def open_with_keyboard(_event=None, target=station_id):
                open_station(target)
                return "break"

            widget.bind("<Return>", open_with_keyboard, add="+")
            widget.bind("<space>", open_with_keyboard, add="+")
            widget.bind(
                "<FocusIn>",
                lambda _event: widget.configure(highlightbackground=palette["accent"]),
                add="+",
            )
            widget.bind(
                "<FocusOut>",
                lambda _event: widget.configure(highlightbackground=palette["outline"]),
                add="+",
            )
            widget.bind(
                "<Enter>",
                lambda _event: widget.configure(highlightbackground=palette["accent"]),
                add="+",
            )
            widget.bind(
                "<Leave>",
                lambda _event: widget.configure(
                    highlightbackground=(
                        palette["accent"] if widget.focus_get() is widget else palette["outline"]
                    )
                ),
                add="+",
            )
        for child in widget.winfo_children():
            bind_station_card(child, station_id, card_root=False)

    def set_auth_status(message: str) -> None:
        auth_status_var.set(message)

    def on_sign_in() -> None:
        if not auth_enabled:
            set_auth_status(
                "Discord sign-in is unavailable because TXITY has not enabled secure authentication."
            )
            return
        auth_manager.start_login()

    def disconnect_discord() -> None:
        nonlocal discord_authenticated, current_page
        pending_live_apply = live_reconfigure_state.get("after_id")
        if pending_live_apply is not None:
            try:
                root.after_cancel(pending_live_apply)
            except tk.TclError:
                pass
        live_reconfigure_state["after_id"] = None
        live_reconfigure_state["request"] = None
        live_reconfigure_state["active_request"] = None
        live_reconfigure_state["stopping"] = False
        live_reconfigure_state["cancelled"] = False
        transmitter.stop(wait=0.5)
        client.set_discord_auth(None, None)
        client.disconnect()
        discord_authenticated = False
        profile_cleared = auth_manager.clear_browser_session()
        account_var.set("Not connected to Discord")
        set_auth_status(
            "Discord was disconnected and the saved browser session was cleared."
            if profile_cleared
            else "Discord was disconnected, but the saved browser session could not be cleared."
        )
        current_page = "transmit"
        build_shell()

    def on_shutdown() -> None:
        pending_live_apply = live_reconfigure_state.get("after_id")
        if pending_live_apply is not None:
            try:
                root.after_cancel(pending_live_apply)
            except tk.TclError:
                pass
            live_reconfigure_state["after_id"] = None
        live_reconfigure_state["request"] = None
        live_reconfigure_state["stopping"] = False
        playlist_watch_stop.set()
        if tray_icon is not None:
            try:
                tray_icon.stop()
            except Exception as exc:
                pass
        if web_gui_server is not None:
            web_gui_server.stop()
        transmitter.stop(wait=1.0)
        client.disconnect()
        save_tx_settings_now()
        auth_manager.close()
        root.destroy()

    def restore_from_tray() -> None:
        def show_window() -> None:
            if tray_icon is not None:
                try:
                    tray_icon.visible = False
                except Exception as exc:
                    pass
            try:
                root.deiconify()
                root.state("normal")
                root.lift()
                root.focus_force()
            except tk.TclError:
                pass

        try:
            root.after(0, show_window)
        except tk.TclError:
            pass

    def quit_from_tray() -> None:
        try:
            root.after(0, on_shutdown)
        except tk.TclError:
            pass

    def initialize_tray_icon(icon: Any) -> None:
        icon.visible = False
        tray_setup_ready.set()

    def minimize_to_tray(attempt: int = 0) -> None:
        if os.name != "nt" or tray_icon is None:
            return
        try:
            if root.state() != "iconic":
                return
            if not tray_setup_ready.is_set():
                if attempt < 40:
                    root.after(50, lambda: minimize_to_tray(attempt + 1))
                return
            tray_icon.visible = True
            root.withdraw()
        except Exception as exc:
            try:
                root.deiconify()
            except tk.TclError:
                pass

    def handle_window_unmap(event: Any) -> None:
        if event.widget is root:
            try:
                root.after_idle(minimize_to_tray)
            except tk.TclError:
                pass

    def toggle_theme() -> None:
        nonlocal theme_mode
        theme_mode = "dark" if theme_mode == "light" else "light"
        apply_current_theme()
        build_shell()

    def navigate(page: str) -> None:
        nonlocal current_page
        if not discord_authenticated and page not in {"discord", "settings", "transmit"}:
            current_page = "discord"
            build_shell()
            return
        current_page = page
        build_shell()

    def open_station(station_id: str) -> None:
        nonlocal selected_station_id, current_page
        selected_station_id = station_id
        if station_directory_window is not None:
            try:
                if station_directory_window.winfo_exists():
                    station_directory_window.destroy()
            except tk.TclError:
                pass
        current_page = "station_detail"
        build_shell()

    def update_station_tree() -> None:
        """Refresh the visible station-card grid."""
        if station_cards_frame is None:
            return
        try:
            if not station_cards_frame.winfo_exists():
                return
        except tk.TclError:
            return

        for child in station_cards_frame.winfo_children():
            child.destroy()

        query = station_search_var.get().strip().casefold()
        visible = []
        for row in station_rows:
            searchable = " ".join(str(value) for value in (
                row.get("station", ""),
                row.get("operator", ""),
                row.get("frequency", ""),
                row.get("mode", ""),
                row.get("description", ""),
            )).casefold()
            if not query or query in searchable:
                visible.append(row)

        station_count_var.set(
            f"{len(visible)} of {len(station_rows)} stations"
            if query else f"{len(station_rows)} stations"
        )

        if not visible:
            empty = make_card(station_cards_frame, padding=16)
            empty.pack(fill="x", padx=6, pady=6)
            status_message = station_status_var.get().strip()
            status = status_message.casefold()
            if "error" in status or "failed" in status:
                message = f"{status_message} Select Refresh to try again."
            elif (
                not users_listing_received
                or status.startswith(("waiting for", "loading", "refreshing"))
            ):
                message = status_message or "Waiting for TXITY station data."
            elif query:
                message = "No stations match that search."
            else:
                message = "TXITY returned an empty station list. Select Refresh to try again."
            make_label(
                empty,
                message,
                color=palette["mutedForeground"],
                
            ).pack(anchor="w")
            return

        for column in range(2):
            station_cards_frame.grid_columnconfigure(column, weight=1, uniform="station-card")

        for index, row in enumerate(visible):
            station_id = str(row.get("id", ""))
            card = make_card(station_cards_frame, padding=12)
            card.grid(
                row=index // 2,
                column=index % 2,
                sticky="nsew",
                padx=6,
                pady=6,
            )
            card.configure(cursor="hand2")

            content = ttk.Frame(card)
            content.pack(fill="both", expand=True)

            logo_url = row.get("logo_url") or ""
            image = make_image_label(content, logo_url, size=112, role=f"station card logo: {row.get('station', 'unknown')}")
            image.pack(side="left", padx=(0, 14), pady=4)

            details = ttk.Frame(content)
            details.pack(side="left", fill="both", expand=True)

            make_label(
                details,
                str(row.get("station", "Unknown station")),
                font=("Segoe UI", 13, "bold"),
                
            ).pack(anchor="w")
            description = str(row.get("description") or "No description yet.")
            make_label(
                details,
                description,
                font=("Segoe UI", 9, "normal"),
                color=palette["mutedForeground"],
                
                wraplength=260,
            ).pack(anchor="w", pady=(4, 7))
            make_label(
                details,
                f"Discord: {row.get('operator') or 'Unknown'}",
                font=("Segoe UI", 8, "normal"),
                color=palette["mutedForeground"],
                
            ).pack(anchor="w")

            meta = ttk.Frame(content)
            meta.pack(side="right", fill="y", padx=(10, 2))
            make_label(
                meta,
                str(row.get("frequency") or "Frequency unknown"),
                font=("Segoe UI", 12, "bold"),
                color=palette["accent"],
                
            ).pack(anchor="e", pady=(4, 8))
            make_label(
                meta,
                str(row.get("rating") or "Not rated"),
                font=("Segoe UI", 9, "bold"),
                
            ).pack(anchor="e", pady=(0, 6))
            make_label(
                meta,
                f"{row.get('mode') or 'Mode unknown'} · {row.get('on_air') or 'On-air time unknown'}",
                font=("Segoe UI", 8, "normal"),
                color=palette["mutedForeground"],
                
                justify="right",
            ).pack(anchor="e")

            bind_station_card(card, station_id)

    def refresh_station_list() -> None:
        if not discord_authenticated:
            navigate("discord")
            return
        station_status_var.set("Refreshing station data from TXITY...")
        if not station_rows:
            update_station_tree()
        client.refresh_stations()

    def show_gate(parent: ttk.Frame) -> None:
        outer = ttk.Frame(parent)
        outer.pack(fill="both", expand=True)

        card = make_card(
            outer,
            
            padding=24,
        )
        card.place(relx=0.5, rely=0.46, anchor="center", width=520)

        make_label(
            card,
            "MultiV",
            font=("Segoe UI", 30, "bold"),
            
            color=palette["accent"],
        ).pack(anchor="w")
        make_label(
            card,
            "Sign in with Discord",
            font=("Segoe UI", 21, "bold"),
            
        ).pack(anchor="w", pady=(14, 6))
        make_label(
            card,
            "MultiV is a multi-source client for virtual radio simulators. "
            "Choose a microphone, desktop audio, or playlist, then transmit through a supported simulator connection. "
            "TXITY is the active connection today.",
            font=("Segoe UI", 10, "normal"),
            color=palette["mutedForeground"],
            
            wraplength=440,
        ).pack(anchor="w", pady=(0, 20))

        make_label(
            card,
            variable=account_var,
            font=("Segoe UI", 11, "bold"),
            
        ).pack(anchor="w", pady=(0, 8))

        status_label = make_label(
            card,
            variable=auth_status_var,
            font=("Segoe UI", 10, "normal"),
            color=palette["mutedForeground"],
            
            wraplength=440,
        )
        status_label.pack(anchor="w", pady=(0, 18))

        make_button(
            card,
            "Sign in with Discord",
            on_sign_in,
            kind="primary",
            padx=20,
            pady=11,
        ).pack(anchor="w")

        make_label(
            card,
            "TXITY authentication is handled by TXITY's official Discord sign-in page.",
            font=("Segoe UI", 9, "normal"),
            color=palette["mutedForeground"],
            
            wraplength=440,
        ).pack(anchor="w", pady=(20, 0))

    def create_scrollable_page(parent: ttk.Frame) -> tuple[ttk.Frame, tk.Canvas]:
        nonlocal active_scroll_canvas
        canvas = tk.Canvas(
            parent,
            background=palette["background"],
            highlightthickness=0,
            borderwidth=0,
            yscrollincrement=SCROLL_UNIT_PIXELS,
        )
        page = ttk.Frame(canvas)
        window_id = canvas.create_window((0, 0), window=page, anchor="nw")

        def update_region(_event=None) -> None:
            bounds = canvas.bbox("all")
            if bounds:
                canvas.configure(scrollregion=bounds)
            canvas.itemconfigure(window_id, width=canvas.winfo_width())

        page.bind("<Configure>", update_region)
        canvas.bind("<Configure>", update_region)
        canvas.pack(fill="both", expand=True)
        active_scroll_canvas = canvas
        return page, canvas

    def show_stations(parent: ttk.Frame) -> None:
        nonlocal station_cards_frame

        page, canvas = create_scrollable_page(parent)
        make_label(
            page,
            "Stations",
            font=("Segoe UI", 27, "bold"),
        ).pack(anchor="w", pady=(6, 2))
        make_label(
            page,
            "Live transmitters running on TXITY.",
            font=("Segoe UI", 10, "normal"),
            color=palette["mutedForeground"],
        ).pack(anchor="w", pady=(0, 17))

        make_label(
            page,
            "Search stations",
            font=("Segoe UI", 9, "bold"),
            color=palette["mutedForeground"],
        ).pack(anchor="w", pady=(0, 5))

        controls = ttk.Frame(page)
        controls.pack(fill="x", pady=(0, 10))

        search_shell = ttk.Frame(controls, padding=5)
        search_shell.pack(side="left", fill="x", expand=True)
        search = ttk.Entry(
            search_shell,
            textvariable=station_search_var,
            font=("Segoe UI", 10),
        )
        search.pack(fill="x", expand=True, ipady=5)
        search.bind("<KeyRelease>", lambda _event: update_station_tree())

        make_button(
            controls,
            "Refresh",
            refresh_station_list,
            kind="primary",
        ).pack(side="left", padx=(10, 0))

        status_bar = ttk.Frame(page)
        status_bar.pack(fill="x", pady=(2, 10))
        make_label(
            status_bar,
            variable=station_status_var,
            font=("Segoe UI", 9, "normal"),
            color=palette["mutedForeground"],
        ).pack(side="left")
        make_label(
            status_bar,
            variable=station_count_var,
            font=("Segoe UI", 9, "bold"),
            color=palette["mutedForeground"],
        ).pack(side="right")

        station_cards_frame = ttk.Frame(page)
        station_cards_frame.pack(fill="x", expand=True)
        update_station_tree()

    def open_station_directory() -> None:
        nonlocal station_directory_window, active_scroll_canvas
        if station_directory_window is not None:
            try:
                if station_directory_window.winfo_exists():
                    station_directory_window.deiconify()
                    station_directory_window.lift()
                    station_directory_window.focus_force()
                    return
            except tk.TclError:
                pass

        previous_canvas = active_scroll_canvas
        dialog = tk.Toplevel(root)
        station_directory_window = dialog
        dialog.title("TXITY station directory")
        dialog.geometry("980x700")
        dialog.minsize(720, 480)
        dialog.configure(background=palette["background"])
        dialog.transient(root)
        show_stations(dialog)

        def close_directory() -> None:
            nonlocal station_directory_window, active_scroll_canvas
            station_directory_window = None
            active_scroll_canvas = previous_canvas
            try:
                dialog.destroy()
            except tk.TclError:
                pass

        dialog.protocol("WM_DELETE_WINDOW", close_directory)
        dialog.bind("<Escape>", lambda _event: close_directory())

    def update_frequency_check() -> None:
        try:
            selected_hz = parse_rf_frequency(tx_frequency_var.get())
        except ValueError:
            tx_frequency_check_var.set("Enter a valid frequency to check the TXITY station list.")
            return

        messages: list[str] = []
        if server_band_min_hz is not None and server_band_max_hz is not None:
            if not server_band_min_hz <= selected_hz <= server_band_max_hz:
                messages.append(
                    "Outside the server's reported virtual frequency range "
                    f"({format_frequency(server_band_min_hz)} to {format_frequency(server_band_max_hz)})."
                )

        own_user_id = str((client.discord_user or {}).get("id") or "")
        other_stations = [
            row for row in station_rows
            if not own_user_id or str(row.get("discord_id") or "") != own_user_id
        ]
        overlaps = find_frequency_overlaps(
            tx_frequency_var.get(),
            tx_mode_var.get(),
            other_stations,
            nfm_profile=tx_nfm_profile_var.get(),
        )

        if overlaps:
            names = []
            for item in overlaps[:4]:
                offset_khz = item["offset_hz"] / 1_000
                names.append(
                    f'{item["station_name"]} at {format_frequency(item["frequency_hz"])} '
                    f'({item["mode"]}, {offset_khz:.1f} kHz offset)'
                )
            extra = f" and {len(overlaps) - 4} more" if len(overlaps) > 4 else ""
            if not tx_allow_overlap_var.get():
                sharing_note = "Frequency sharing is off, so TXITY may reject this overlap."
            elif any(item["allow_overlap_visitors"] is False for item in overlaps):
                sharing_note = "At least one overlapping station reports sharing disabled."
            elif all(item["allow_overlap_visitors"] is True for item in overlaps):
                sharing_note = "The listed stations report sharing enabled."
            else:
                sharing_note = "Remote sharing settings are not fully public; TXITY makes the final decision."
            messages.append(
                "Estimated overlap with " + "; ".join(names) + extra + ". " + sharing_note
            )
        elif station_rows:
            messages.append("No estimated channel overlap with the other listed TXITY stations.")
        else:
            messages.append("Station overlap check will run when TXITY's live station list loads.")

        fallback_count = sum(
            1 for variable in tx_frequency_fallback_vars if variable.get().strip()
        )
        if fallback_count:
            frequency_word = "frequency" if fallback_count == 1 else "frequencies"
            messages.append(
                f"MultiV will try {fallback_count} alternate {frequency_word} if TXITY rejects the first frequency."
            )
        messages.append("MultiV estimates overlap from the station list. TXITY makes the final decision about shared frequencies.")
        tx_frequency_check_var.set(" ".join(messages))

    tx_frequency_var.trace_add("write", lambda *_args: update_frequency_check())
    for fallback_variable in tx_frequency_fallback_vars:
        fallback_variable.trace_add("write", lambda *_args: update_frequency_check())
    tx_mode_var.trace_add("write", lambda *_args: update_frequency_check())
    tx_allow_overlap_var.trace_add("write", lambda *_args: update_frequency_check())

    live_reconfigure_state: dict[str, Any] = {
        "after_id": None,
        "request": None,
        "active_request": None,
        "stopping": False,
        "cancelled": False,
    }

    def update_transmit_button() -> None:
        button = tx_transmit_button
        if button is None:
            return
        try:
            if button.winfo_exists():
                button.configure(
                    text=(
                        "Stop Transmission"
                        if transmitter.is_busy or live_reconfigure_state["stopping"]
                        else "Start Transmission"
                    )
                )
        except tk.TclError:
            pass

    def build_transmission_request() -> dict[str, Any]:
        config = build_transmit_config(
            station_name=tx_station_name_var.get(),
            description=tx_description_var.get(),
            frequency=tx_frequency_var.get(),
            mode=tx_mode_var.get(),
            station_dbm=tx_power_var.get(),
            allow_overlap=tx_allow_overlap_var.get(),
            auth_token=client.auth_token,
            cw_text=tx_cw_text_var.get(),
            cw_wpm=tx_cw_wpm_var.get(),
            cw_loop=tx_cw_loop_var.get(),
            station_logo_url=tx_logo_url_var.get(),
            station_details=tx_details_var.get(),
            station_page_layout=tx_page_layout_var.get(),
            fade_enabled=tx_fade_enabled_var.get(),
            fade_seconds=tx_fade_seconds_var.get(),
            fade_min_dbm=tx_fade_min_dbm_var.get(),
            fade_shape=tx_fade_shape_var.get().split("-", 1)[0].strip(),
            agc_enabled=tx_agc_enabled_var.get(),
            agc_target=tx_agc_target_var.get(),
            mic_gain_db=tx_mic_gain_var.get(),
            bass_db=tx_bass_var.get(),
            treble_db=tx_treble_var.get(),
            presence_db=tx_presence_var.get(),
            drive=tx_drive_var.get(),
            nfm_transport_bandwidth_hz=tx_nfm_transport_bandwidth_var.get(),
            nfm_profile=tx_nfm_profile_var.get(),
            ssb_bandwidth_hz=tx_ssb_bandwidth_var.get(),
            ssb_low_cut_hz=tx_ssb_low_cut_var.get(),
            ssb_high_cut_hz=tx_ssb_high_cut_var.get(),
            am_bandwidth_hz=tx_am_bandwidth_var.get(),
            am_pre_emphasis=tx_am_pre_emphasis_var.get(),
            drm_service_label=tx_drm_service_label_var.get(),
            drm_service_id=tx_drm_service_id_var.get(),
            drm_language=tx_drm_language_var.get(),
            drm_programme_type=tx_drm_programme_type_var.get(),
            drm_iso_language=tx_drm_iso_language_var.get(),
            drm_iso_country=tx_drm_iso_country_var.get(),
            drm_quality_profile=tx_drm_quality_profile_var.get(),
            drm_audio_codec=tx_drm_audio_codec_var.get(),
            drm_msc_mode=tx_drm_msc_mode_var.get(),
            drm_stereo=tx_drm_stereo_var.get(),
            drm_interleaving=tx_drm_interleaving_var.get().split(" ", 1)[0].casefold(),
        )
        if config["mode"] == "CW":
            audio_source = "mic"
        else:
            audio_source = {
                "Desktop audio (output loopback)": "loopback",
                "Audio playlist": "playlist",
            }.get(tx_audio_source_var.get(), "mic")

        if config.get("_txitpyMode") == "DRM" and audio_source == "loopback":
            raise ValueError(
                "DRM mode supports microphone and playlist inputs, not desktop audio loopback."
            )

        txity_enabled = bool(txity_output_enabled_var.get())
        icecast_enabled = bool(icecast_output_enabled_var.get())
        if not txity_enabled and not icecast_enabled:
            raise ValueError("Enable at least one output: TXITY or Icecast.")

        selected_icecast_destinations = []
        if icecast_enabled:
            for destination in icecast_destinations:
                if not bool(destination.get("enabled", True)):
                    continue
                if not str(destination.get("url") or "").strip() and not str(
                    destination.get("password") or ""
                ):
                    continue
                selected_icecast_destinations.append(
                    normalize_icecast_destination(destination)
                )

        selected_server_format = tx_icecast_server_format_var.get().strip().casefold()
        server_format_spec = ICECAST_FORMATS.get(selected_server_format)
        if server_format_spec is None:
            raise ValueError("Choose an available built-in Icecast audio format.")
        if server_format_spec.get("lossless"):
            icecast_bitrate_kbps = 128
            built_in_server_bitrate = 0
        else:
            try:
                built_in_server_bitrate = int(tx_icecast_bitrate_var.get())
            except (TypeError, ValueError) as exc:
                raise ValueError("Built-in Icecast bitrate must be a whole number.") from exc
            if built_in_server_bitrate not in ICECAST_BITRATES:
                raise ValueError(
                    "Built-in Icecast bitrate must be one of: "
                    + ", ".join(str(value) for value in ICECAST_BITRATES)
                    + " kbps."
                )
            if selected_server_format == "aac_plus" and built_in_server_bitrate > 128:
                raise ValueError("AAC+ bitrate must be 128 kbps or lower.")
            if selected_server_format == "ogg_opus" and built_in_server_bitrate < 48:
                raise ValueError("Ogg Opus bitrate must be at least 48 kbps.")
            icecast_bitrate_kbps = built_in_server_bitrate

        try:
            icecast_server_port = int(tx_icecast_server_port_var.get())
            icecast_server_max_listeners = int(tx_icecast_server_max_listeners_var.get())
        except (TypeError, ValueError) as exc:
            raise ValueError("Built-in Icecast port and listener limit must be whole numbers.") from exc
        icecast_server_mount = tx_icecast_server_mount_var.get().strip()
        if not 1 <= icecast_server_port <= 65535:
            raise ValueError("Built-in Icecast port must be between 1 and 65535.")
        if not 1 <= icecast_server_max_listeners <= 500:
            raise ValueError("Built-in Icecast listener limit must be between 1 and 500.")
        if not icecast_server_mount.startswith("/") or any(
            char.isspace() or ord(char) < 32 for char in icecast_server_mount
        ):
            raise ValueError("Built-in Icecast mountpoint must start with / and contain no spaces.")
        built_in_server_enabled = (
            icecast_enabled and bool(tx_icecast_server_enabled_var.get())
        )
        icecast_server_settings = {
            "enabled": built_in_server_enabled,
            "host": "0.0.0.0",
            "port": icecast_server_port,
            "mount": icecast_server_mount,
            "listener_password": tx_icecast_server_password_var.get(),
            "max_listeners": icecast_server_max_listeners,
            "audio_format": selected_server_format,
            "bitrate_kbps": built_in_server_bitrate,
        }
        if not txity_enabled and not selected_icecast_destinations and not built_in_server_enabled:
            raise ValueError("Enable the Icecast server or add an enabled remote Icecast destination.")

        playlist_files = list(tx_playlist_files) if audio_source == "playlist" else []
        selected_device = (
            tx_audio_device_ids.get(tx_audio_device_var.get())
            if audio_source in {"mic", "loopback"}
            else None
        )
        if config["mode"] != "CW" and selected_device is None and audio_source == "loopback":
            raise ValueError("Select a desktop output device for loopback capture.")
        if audio_source == "playlist" and not playlist_files:
            raise ValueError("Add at least one audio file or folder to the playlist.")

        candidates = build_frequency_candidates(
            tx_frequency_var.get(),
            [variable.get() for variable in tx_frequency_fallback_vars],
        )
        apply_nfm_transport(config, candidates[0][1])
        apply_rade_transport(config, candidates[0][1])

        playlist_shuffle = tx_playlist_shuffle_var.get()
        playlist_loop = tx_playlist_loop_var.get()
        retry_context = {
            "base_config": dict(config),
            "candidates": candidates,
            "next_index": 1,
            "current_index": 0,
            "pending": False,
            "input_device": selected_device,
            "audio_source": audio_source,
            "playlist_files": list(playlist_files),
            "playlist_shuffle": playlist_shuffle,
            "playlist_loop": playlist_loop,
            "icecast_destinations": list(selected_icecast_destinations),
            "icecast_bitrate_kbps": icecast_bitrate_kbps,
            "icecast_server_settings": dict(icecast_server_settings),
            "txity_enabled": txity_enabled,
        }
        return {
            "config": config,
            "input_device": selected_device,
            "audio_source": audio_source,
            "playlist_files": playlist_files,
            "playlist_shuffle": playlist_shuffle,
            "playlist_loop": playlist_loop,
            "icecast_destinations": list(selected_icecast_destinations),
            "icecast_bitrate_kbps": icecast_bitrate_kbps,
            "icecast_server_settings": dict(icecast_server_settings),
            "txity_enabled": txity_enabled,
            "frequency_retry_context": retry_context,
        }

    def start_transmission_request(request: dict[str, Any]) -> None:
        nonlocal frequency_retry_context
        frequency_retry_context = request["frequency_retry_context"]
        transmitter.start(
            request["config"],
            input_device=request["input_device"],
            audio_source=request["audio_source"],
            playlist_files=request["playlist_files"],
            playlist_shuffle=request["playlist_shuffle"],
            playlist_loop=request["playlist_loop"],
            icecast_destinations=request["icecast_destinations"],
            icecast_bitrate_kbps=request["icecast_bitrate_kbps"],
            icecast_server_settings=request["icecast_server_settings"],
            txity_enabled=request["txity_enabled"],
        )
        live_reconfigure_state["active_request"] = request
        tx_status_var.set("Preparing transmission...")
        if request["audio_source"] == "playlist":
            tx_playlist_status_var.set("Preparing playlist...")
        update_transmit_button()

    def _request_matches(left: dict[str, Any] | None, right: dict[str, Any]) -> bool:
        if not isinstance(left, dict):
            return False
        keys = (
            "config",
            "input_device",
            "audio_source",
            "playlist_files",
            "playlist_shuffle",
            "playlist_loop",
            "icecast_destinations",
            "icecast_bitrate_kbps",
            "icecast_server_settings",
            "txity_enabled",
        )
        return all(left.get(key) == right.get(key) for key in keys)

    def apply_live_reconfigure() -> None:
        nonlocal frequency_retry_context
        live_reconfigure_state["after_id"] = None
        if not transmitter.is_busy and not live_reconfigure_state["stopping"]:
            return
        if drm_setup_state["running"]:
            try:
                live_reconfigure_state["after_id"] = root.after(500, apply_live_reconfigure)
            except tk.TclError:
                live_reconfigure_state["after_id"] = None
            return

        try:
            request = build_transmission_request()
        except Exception as exc:
            tx_status_var.set(
                "Live change not applied: " + safe_error_text(str(exc), 380)
            )
            return

        if (
            not live_reconfigure_state["stopping"]
            and _request_matches(live_reconfigure_state["active_request"], request)
        ):
            return

        live_reconfigure_state["request"] = request
        if live_reconfigure_state["stopping"]:
            return

        live_reconfigure_state["stopping"] = True
        live_reconfigure_state["cancelled"] = False
        frequency_retry_context = None
        tx_status_var.set("Applying changes. Transmission will briefly pause.")
        if request["audio_source"] == "playlist":
            tx_playlist_status_var.set("Restarting playlist with updated settings...")
        transmitter.stop()
        update_transmit_button()

    def schedule_live_reconfigure(*_args) -> None:
        if not transmitter.is_busy and not live_reconfigure_state["stopping"]:
            return
        previous = live_reconfigure_state["after_id"]
        if previous is not None:
            try:
                root.after_cancel(previous)
            except tk.TclError:
                pass
        try:
            # Sliders emit many writes while dragging. Apply one update after
            # the user has paused, rather than restarting on every slider tick.
            live_reconfigure_state["after_id"] = root.after(
                900,
                apply_live_reconfigure,
            )
        except tk.TclError:
            live_reconfigure_state["after_id"] = None

    def cancel_pending_live_reconfigure() -> None:
        if live_reconfigure_state["stopping"]:
            live_reconfigure_state["request"] = None
            live_reconfigure_state["cancelled"] = True

    def start_pending_live_reconfigure() -> None:
        nonlocal frequency_retry_context
        request = live_reconfigure_state["request"]
        live_reconfigure_state["request"] = None
        live_reconfigure_state["stopping"] = False
        cancelled = bool(live_reconfigure_state["cancelled"])
        live_reconfigure_state["cancelled"] = False

        if request is None or cancelled:
            live_reconfigure_state["active_request"] = None
            update_transmit_button()
            return

        try:
            start_transmission_request(request)
        except Exception as exc:
            live_reconfigure_state["active_request"] = None
            frequency_retry_context = None
            tx_status_var.set(
                "Could not apply live changes: " + safe_error_text(str(exc), 450)
            )
            update_transmit_button()

    def toggle_transmission() -> None:
        nonlocal frequency_retry_context
        if live_reconfigure_state["stopping"]:
            # A second click during the automatic restart cancels the restart;
            # the current transmitter has already received its stop request.
            live_reconfigure_state["request"] = None
            live_reconfigure_state["cancelled"] = True
            frequency_retry_context = None
            tx_status_var.set("Stopping transmission...")
            update_transmit_button()
            return

        pending_after = live_reconfigure_state["after_id"]
        if pending_after is not None:
            try:
                root.after_cancel(pending_after)
            except tk.TclError:
                pass
            live_reconfigure_state["after_id"] = None

        if transmitter.is_busy:
            live_reconfigure_state["request"] = None
            live_reconfigure_state["active_request"] = None
            live_reconfigure_state["cancelled"] = False
            frequency_retry_context = None
            transmitter.stop()
            tx_status_var.set("Stopping transmission...")
            if tx_audio_source_var.get() == "Audio playlist":
                tx_playlist_status_var.set("Stopping playlist...")
            update_transmit_button()
            return
        if txity_output_enabled_var.get() and not discord_authenticated:
            navigate("discord")
            return

        live_reconfigure_state["request"] = None
        live_reconfigure_state["cancelled"] = False
        try:
            request = build_transmission_request()
            start_transmission_request(request)
        except Exception as exc:
            frequency_retry_context = None
            details = safe_error_text(str(exc), 500)
            tx_status_var.set(details)
            update_transmit_button()

    for setting_variable in tx_setting_variables:
        if setting_variable is not web_gui_enabled_var:
            setting_variable.trace_add("write", schedule_live_reconfigure)
    for fallback_variable in tx_frequency_fallback_vars:
        fallback_variable.trace_add("write", schedule_live_reconfigure)

    def get_web_gui_state() -> dict[str, Any]:
        with web_gui_state_lock:
            return dict(web_gui_state)

    def publish_web_gui_state() -> None:
        snapshot = {
            "station_name": tx_station_name_var.get(),
            "description": tx_description_var.get(),
            "frequency": tx_frequency_var.get(),
            "mode": tx_mode_var.get(),
            "station_dbm": tx_power_var.get(),
            "allow_overlap": tx_allow_overlap_var.get(),
            "station_logo_url": tx_logo_url_var.get(),
            "station_details": tx_details_var.get(),
            "audio_source": tx_audio_source_var.get(),
            "status": tx_status_var.get(),
            "transmitting": transmitter.is_busy,
            "account": account_var.get(),
            "notice": web_gui_status_var.get(),
        }
        with web_gui_state_lock:
            web_gui_state.clear()
            web_gui_state.update(snapshot)

    def poll_web_gui_commands() -> None:
        while True:
            try:
                command = web_gui_commands.get_nowait()
            except queue.Empty:
                break

            command_type = command.get("type")
            if command_type == "settings":
                values = command.get("values", {})
                if not isinstance(values, dict):
                    continue
                tx_station_name_var.set(str(values.get("station_name", ""))[:48])
                tx_description_var.set(str(values.get("description", ""))[:180])
                tx_frequency_var.set(str(values.get("frequency", ""))[:32])
                selected_mode = str(values.get("mode", "AM")).upper()
                tx_mode_var.set(selected_mode)
                tx_mode_display_var.set(
                    "DRM [EXPERIMENTAL]" if selected_mode == "DRM" else selected_mode
                )
                tx_power_var.set(str(values.get("station_dbm", "50"))[:16])
                tx_allow_overlap_var.set(bool(values.get("allow_overlap", False)))
                tx_logo_url_var.set(str(values.get("station_logo_url", ""))[:512])
                tx_details_var.set(str(values.get("station_details", ""))[:1200])
                save_tx_settings_now()
                web_gui_status_var.set("Station settings updated from the browser.")
            elif command_type == "transmit":
                action = command.get("action")
                if action == "start":
                    if transmitter.is_busy:
                        web_gui_status_var.set("A transmission is already starting or active.")
                    elif txity_output_enabled_var.get() and not discord_authenticated:
                        web_gui_status_var.set(
                            "Sign in to Discord in the MultiV desktop app before sending to TXITY."
                        )
                    else:
                        toggle_transmission()
                        web_gui_status_var.set("Transmission start requested.")
                elif action == "stop":
                    if transmitter.is_busy:
                        toggle_transmission()
                        web_gui_status_var.set("Transmission stop requested.")
                    else:
                        web_gui_status_var.set("No transmission is active.")

        publish_web_gui_state()
        try:
            root.after(150, poll_web_gui_commands)
        except tk.TclError:
            pass

    def start_web_gui_server() -> None:
        nonlocal web_gui_server
        if web_gui_server is not None and web_gui_server.running:
            web_gui_status_var.set("Listening on http://0.0.0.0:23234")
            return
        if not web_gui_password_salt or not web_gui_password_hash:
            raise ValueError("Set a Web GUI password before enabling the browser interface.")
        server = TxityWebGUI(
            host="0.0.0.0",
            port=23234,
            password_salt=web_gui_password_salt,
            password_hash=web_gui_password_hash,
            state_provider=get_web_gui_state,
            command_submitter=web_gui_commands.put,
        )
        server.start()
        web_gui_server = server
        web_gui_status_var.set("Listening on http://0.0.0.0:23234")

    def stop_web_gui_server() -> None:
        nonlocal web_gui_server
        if web_gui_server is not None:
            web_gui_server.stop()
            web_gui_server = None
        web_gui_status_var.set("Web GUI is disabled.")

    def save_web_gui_password() -> None:
        nonlocal web_gui_password_salt, web_gui_password_hash
        try:
            web_gui_password_salt, web_gui_password_hash = create_password_record(
                web_gui_password_var.get()
            )
        except ValueError as exc:
            messagebox.showerror("Web GUI password", str(exc), parent=root)
            return
        web_gui_password_var.set("")
        if web_gui_server is not None:
            web_gui_server.set_credentials(web_gui_password_salt, web_gui_password_hash)
        save_tx_settings_now()
        if web_gui_enabled_var.get() and (web_gui_server is None or not web_gui_server.running):
            try:
                start_web_gui_server()
            except (OSError, ValueError) as exc:
                web_gui_enabled_var.set(False)
                save_tx_settings_now()
                web_gui_status_var.set(f"Could not start Web GUI: {exc}")
                messagebox.showerror("Web GUI", str(exc), parent=root)
                return
        messagebox.showinfo(
            "Web GUI password",
            "Password saved as a salted PBKDF2 hash.",
            parent=root,
        )

    def toggle_web_gui() -> None:
        if web_gui_enabled_var.get():
            if not web_gui_password_salt or not web_gui_password_hash:
                web_gui_enabled_var.set(False)
                save_tx_settings_now()
                messagebox.showinfo(
                    "Web GUI password required",
                    "Set a Web GUI password before enabling the browser interface.",
                    parent=root,
                )
                return
            try:
                start_web_gui_server()
            except (OSError, ValueError) as exc:
                web_gui_enabled_var.set(False)
                save_tx_settings_now()
                web_gui_status_var.set(f"Could not start Web GUI: {exc}")
                messagebox.showerror("Web GUI", str(exc), parent=root)
                return
        else:
            stop_web_gui_server()
        save_tx_settings_now()

    def start_pending_frequency_retry() -> None:
        nonlocal frequency_retry_context
        nonlocal tx_negative_status_reason, tx_negative_status_count
        nonlocal tx_ptt_confirmed, tx_terminal_server_message

        context = frequency_retry_context
        if not isinstance(context, dict) or not context.get("pending"):
            return

        candidate = context.pop("queued_candidate", None)
        context["pending"] = False
        if not isinstance(candidate, tuple) or len(candidate) != 2:
            frequency_retry_context = None
            tx_status_var.set("Could not prepare the alternate frequency.")
            update_transmit_button()
            return

        display_text, frequency_hz = candidate
        retry_config = dict(context["base_config"])
        apply_nfm_transport(retry_config, frequency_hz)
        apply_rade_transport(retry_config, frequency_hz)
        context["current_index"] = int(context["next_index"]) - 1

        tx_negative_status_reason = None
        tx_negative_status_count = 0
        tx_ptt_confirmed = False
        tx_terminal_server_message = None
        tx_status_var.set(
            f"Trying frequency {context['current_index'] + 1} of "
            f"{len(context['candidates'])}: {display_text}."
        )
        if context["audio_source"] == "playlist":
            tx_playlist_status_var.set("Preparing playlist...")
        try:
            transmitter.start(
                retry_config,
                input_device=context["input_device"],
                audio_source=context["audio_source"],
                playlist_files=context["playlist_files"],
                playlist_shuffle=context["playlist_shuffle"],
                playlist_loop=context["playlist_loop"],
                icecast_destinations=context.get("icecast_destinations", []),
                icecast_bitrate_kbps=context.get("icecast_bitrate_kbps", 128),
                icecast_server_settings=context.get("icecast_server_settings", {}),
                txity_enabled=bool(context.get("txity_enabled", True)),
            )
        except Exception as exc:
            frequency_retry_context = None
            tx_status_var.set(
                f"Could not start alternate frequency {display_text}: "
                f"{safe_error_text(str(exc), 500)}"
            )
        update_transmit_button()

    def show_transmit(parent: ttk.Frame) -> None:
        nonlocal tx_transmit_button, playlist_ui_refresh_callback

        page, _canvas = create_scrollable_page(parent)

        hero = ttk.Frame(page)
        hero.pack(fill="x", pady=(6, 16))
        make_label(
            hero,
            "MultiV",
            font=("Segoe UI", 28, "bold"),
        ).pack(side="left")
        make_label(
            hero,
            "Transmit",
            font=("Segoe UI", 11, "normal"),
            color=palette["mutedForeground"],
        ).pack(side="left", padx=(12, 0), pady=(9, 0))
        make_label(
            hero,
            variable=connection_var,
            font=("Segoe UI", 9, "normal"),
            color=palette["mutedForeground"],
        ).pack(side="right", pady=(10, 0))
        tx_transmit_button = make_button(
            hero,
            "Stop Transmission" if transmitter.is_busy else "Start Transmission",
            toggle_transmission,
            kind="primary",
            padx=18,
            pady=9,
        )
        tx_transmit_button.pack(side="right", padx=(12, 0))
        make_label(
            hero,
            variable=tx_status_var,
            font=("Segoe UI", 9, "bold"),
            color=palette["mutedForeground"],
            wraplength=440,
            justify="right",
        ).pack(side="right", padx=(10, 0), pady=(4, 0))

        def add_tx_entry(
            parent_frame: tk.Misc,
            label: str,
            variable: tk.StringVar,
            *,
            column: int,
            row: int,
        ) -> ttk.Entry:
            wrapper = ttk.Frame(parent_frame)
            wrapper.grid(row=row, column=column, sticky="ew", padx=6, pady=6)
            make_label(
                wrapper,
                label,
                font=("Segoe UI", 9, "bold"),
                color=palette["mutedForeground"],
                
            ).pack(anchor="w", pady=(0, 4))
            entry = ttk.Entry(
                wrapper,
                textvariable=variable,
                font=("Segoe UI", 10),
            )
            entry.pack(fill="x", ipady=7)
            return entry

        def style_tx_combobox(widget: ttk.Combobox) -> None:
            # Keep the native Sun Valley combobox layout and states.
            widget.configure(style="TCombobox", font=("Segoe UI", 10))
            widget.bind("<MouseWheel>", scroll_active_canvas)
            widget.bind("<Button-4>", lambda event: scroll_active_canvas(event, -1))
            widget.bind("<Button-5>", lambda event: scroll_active_canvas(event, 1))

        station_card = make_card(page, padding=16)
        station_card.pack(fill="x", pady=(0, 12))
        make_label(
            station_card,
            "Station",
            font=("Segoe UI", 15, "bold"),
            
        ).pack(anchor="w", pady=(0, 10))

        station_fields = ttk.Frame(station_card)
        station_fields.pack(fill="x")
        station_fields.grid_columnconfigure(0, weight=1, uniform="station-fields")
        station_fields.grid_columnconfigure(1, weight=1, uniform="station-fields")
        add_tx_entry(station_fields, "Station name (required)", tx_station_name_var, column=0, row=0)
        add_tx_entry(station_fields, "Short description", tx_description_var, column=1, row=0)

        frequency_fields_frame = ttk.Frame(
            station_fields,
        )
        frequency_fields_frame.grid(
            row=1,
            column=0,
            columnspan=2,
            sticky="ew",
            padx=6,
            pady=6,
        )
        frequency_fields_frame.grid_columnconfigure(0, weight=1)

        def remove_frequency_fallback(variable: tk.StringVar) -> None:
            if variable in tx_frequency_fallback_vars:
                tx_frequency_fallback_vars.remove(variable)
                render_frequency_fields()
                schedule_tx_settings_save()
                update_frequency_check()
                schedule_live_reconfigure()

        def add_frequency_fallback() -> None:
            if len(tx_frequency_fallback_vars) >= 7:
                tx_status_var.set("You can add up to seven alternate frequencies.")
                return
            variable = tk.StringVar(root, "")
            variable.trace_add("write", schedule_tx_settings_save)
            variable.trace_add("write", lambda *_args: update_frequency_check())
            variable.trace_add("write", schedule_live_reconfigure)
            tx_frequency_fallback_vars.append(variable)
            render_frequency_fields()
            schedule_tx_settings_save()
            update_frequency_check()

        def render_frequency_fields() -> None:
            for child in frequency_fields_frame.winfo_children():
                child.destroy()

            frequency_rows = [
                ("Transmit frequency", tx_frequency_var, None),
                *[
                    (f"Fallback frequency {index}", variable, variable)
                    for index, variable in enumerate(tx_frequency_fallback_vars, start=1)
                ],
            ]
            for index, (label_text, variable, removable) in enumerate(frequency_rows):
                wrapper = ttk.Frame(
                    frequency_fields_frame,
                )
                wrapper.pack(fill="x", pady=(0, 8 if index < len(frequency_rows) - 1 else 0))
                make_label(
                    wrapper,
                    label_text,
                    font=("Segoe UI", 9, "bold"),
                    color=palette["mutedForeground"],
                    
                ).pack(anchor="w", pady=(0, 4))

                input_row = ttk.Frame(wrapper)
                input_row.pack(fill="x")
                entry = ttk.Entry(
                    input_row,
                    textvariable=variable,
                    font=("Segoe UI", 10),
                )
                entry.pack(side="left", fill="x", expand=True, ipady=7)

                if removable is None:
                    make_button(
                        input_row,
                        "+",
                        add_frequency_fallback,
                        kind="tonal",
                        padx=13,
                        pady=6,
                    ).pack(side="left", padx=(8, 0))
                else:
                    make_button(
                        input_row,
                        "−",
                        lambda target=removable: remove_frequency_fallback(target),
                        kind="text",
                        padx=12,
                        pady=6,
                    ).pack(side="left", padx=(8, 0))

        render_frequency_fields()
        make_label(
            station_card,
            variable=tx_frequency_check_var,
            font=("Segoe UI", 9, "normal"),
            color=palette["mutedForeground"],
            
            wraplength=860,
        ).pack(anchor="w", padx=6, pady=(8, 0))
        update_frequency_check()

        signal_card = make_card(page, padding=16)
        signal_card.pack(fill="x", pady=(0, 12))

        advanced_container = ttk.Frame(page)
        advanced_expanded = False
        drm_metadata_card = make_card(advanced_container, padding=16)
        make_label(
            drm_metadata_card,
            "DRM service metadata",
            font=("Segoe UI", 15, "bold"),
            
        ).pack(anchor="w", pady=(0, 4))
        make_label(
            drm_metadata_card,
            "These values are broadcast in the DRM service information. A blank service label uses the station name.",
            font=("Segoe UI", 9, "normal"),
            color=palette["mutedForeground"],
            
            wraplength=860,
        ).pack(anchor="w", pady=(0, 10))

        drm_metadata_fields = ttk.Frame(drm_metadata_card)
        drm_metadata_fields.pack(fill="x")
        drm_metadata_fields.grid_columnconfigure(0, weight=1, uniform="drm-metadata")
        drm_metadata_fields.grid_columnconfigure(1, weight=1, uniform="drm-metadata")
        add_tx_entry(
            drm_metadata_fields,
            "Service label (optional, max 16 characters)",
            tx_drm_service_label_var,
            column=0,
            row=0,
        )
        add_tx_entry(
            drm_metadata_fields,
            "Service ID (hexadecimal, 24-bit)",
            tx_drm_service_id_var,
            column=1,
            row=0,
        )

        language_frame = ttk.Frame(drm_metadata_fields)
        language_frame.grid(row=1, column=0, sticky="ew", padx=6, pady=6)
        make_label(
            language_frame,
            "Language",
            font=("Segoe UI", 9, "bold"),
            color=palette["mutedForeground"],
            
        ).pack(anchor="w", pady=(0, 4))
        drm_language_picker = ttk.Combobox(
            language_frame,
            textvariable=tx_drm_language_var,
            values=DRM_LANGUAGES,
            state="readonly",
            height=10,
        )
        drm_language_picker.pack(fill="x", ipady=5)
        style_tx_combobox(drm_language_picker)

        programme_type_frame = ttk.Frame(drm_metadata_fields)
        programme_type_frame.grid(row=1, column=1, sticky="ew", padx=6, pady=6)
        make_label(
            programme_type_frame,
            "Programme type",
            font=("Segoe UI", 9, "bold"),
            color=palette["mutedForeground"],
            
        ).pack(anchor="w", pady=(0, 4))
        drm_programme_type_picker = ttk.Combobox(
            programme_type_frame,
            textvariable=tx_drm_programme_type_var,
            values=DRM_PROGRAMME_TYPES,
            state="readonly",
            height=12,
        )
        drm_programme_type_picker.pack(fill="x", ipady=5)
        style_tx_combobox(drm_programme_type_picker)

        add_tx_entry(
            drm_metadata_fields,
            "ISO language code (optional, e.g. eng)",
            tx_drm_iso_language_var,
            column=0,
            row=2,
        )
        add_tx_entry(
            drm_metadata_fields,
            "Country code (optional, e.g. US)",
            tx_drm_iso_country_var,
            column=1,
            row=2,
        )

        make_label(
            drm_metadata_card,
            "Audio settings",
            font=("Segoe UI", 12, "bold"),
            
        ).pack(anchor="w", pady=(14, 8))

        drm_audio_fields = ttk.Frame(drm_metadata_card)
        drm_audio_fields.pack(fill="x")
        drm_audio_fields.grid_columnconfigure(0, weight=1, uniform="drm-audio")
        drm_audio_fields.grid_columnconfigure(1, weight=1, uniform="drm-audio")

        codec_frame = ttk.Frame(drm_audio_fields)
        codec_frame.grid(row=0, column=0, sticky="ew", padx=6, pady=6)
        make_label(
            codec_frame,
            "Audio codec",
            font=("Segoe UI", 9, "bold"),
            color=palette["mutedForeground"],
            
        ).pack(anchor="w", pady=(0, 4))
        drm_codec_picker = ttk.Combobox(
            codec_frame,
            textvariable=tx_drm_audio_codec_var,
            values=DRM_AUDIO_CODECS,
            state="readonly",
            height=4,
        )
        drm_codec_picker.pack(fill="x", ipady=5)
        style_tx_combobox(drm_codec_picker)
        make_label(
            codec_frame,
            "xHE-AAC is designed for better quality at low bitrates. Use HE-AAC for older decoders.",
            font=("Segoe UI", 9, "normal"),
            color=palette["mutedForeground"],
            
            wraplength=400,
        ).pack(anchor="w", pady=(4, 0))

        interleaving_frame = ttk.Frame(drm_audio_fields)
        interleaving_frame.grid(row=0, column=1, sticky="ew", padx=6, pady=6)
        make_label(
            interleaving_frame,
            "Interleaving",
            font=("Segoe UI", 9, "bold"),
            color=palette["mutedForeground"],
            
        ).pack(anchor="w", pady=(0, 4))
        drm_interleaving_picker = ttk.Combobox(
            interleaving_frame,
            textvariable=tx_drm_interleaving_var,
            values=("Long (2 s)", "Short (400 ms)"),
            state="readonly",
            height=4,
        )
        drm_interleaving_picker.pack(fill="x", ipady=5)
        style_tx_combobox(drm_interleaving_picker)
        make_label(
            interleaving_frame,
            "Long interleaving improves resistance to fading, with a little more decoding delay.",
            font=("Segoe UI", 9, "normal"),
            color=palette["mutedForeground"],
            
            wraplength=400,
        ).pack(anchor="w", pady=(4, 0))

        msc_frame = ttk.Frame(drm_audio_fields)
        msc_frame.grid(row=1, column=0, sticky="ew", padx=6, pady=6)
        make_label(
            msc_frame,
            "MSC modulation",
            font=("Segoe UI", 9, "bold"),
            color=palette["mutedForeground"],
            
        ).pack(anchor="w", pady=(0, 4))
        drm_msc_picker = ttk.Combobox(
            msc_frame,
            textvariable=tx_drm_msc_mode_var,
            values=DRM_MSC_MODES,
            state="readonly",
            height=4,
        )
        drm_msc_picker.pack(fill="x", ipady=5)
        style_tx_combobox(drm_msc_picker)
        make_label(
            msc_frame,
            "16-QAM is more robust but carries less audio data. 64-QAM has more capacity when reception is strong.",
            font=("Segoe UI", 9, "normal"),
            color=palette["mutedForeground"],
            
            wraplength=400,
        ).pack(anchor="w", pady=(4, 0))

        def apply_drm_quality_profile(_event=None) -> None:
            try:
                profile = drm_quality_profile_settings(tx_drm_quality_profile_var.get())
            except ValueError:
                return
            tx_drm_msc_mode_var.set(str(profile["msc_mode"]))
            tx_drm_stereo_var.set(bool(profile["stereo"]))
            tx_drm_interleaving_var.set("Long (2 s)")

        drm_profile_frame = ttk.Frame(drm_audio_fields)
        drm_profile_frame.grid(row=2, column=0, sticky="ew", padx=6, pady=6)
        make_label(
            drm_profile_frame,
            "Quality profile",
            font=("Segoe UI", 9, "bold"),
            color=palette["mutedForeground"],
            
        ).pack(anchor="w", pady=(0, 4))
        drm_profile_picker = ttk.Combobox(
            drm_profile_frame,
            textvariable=tx_drm_quality_profile_var,
            values=DRM_QUALITY_PROFILES,
            state="readonly",
            height=5,
        )
        drm_profile_picker.pack(fill="x", ipady=5)
        style_tx_combobox(drm_profile_picker)
        drm_profile_picker.bind("<<ComboboxSelected>>", apply_drm_quality_profile)
        make_label(
            drm_profile_frame,
            "The preset sets channel width, MSC modulation, stereo, and long interleaving. You can change MSC modulation and stereo below.",
            font=("Segoe UI", 9, "normal"),
            color=palette["mutedForeground"],
            
            wraplength=400,
        ).pack(anchor="w", pady=(4, 0))

        stereo_frame = ttk.Frame(drm_audio_fields)
        stereo_frame.grid(row=2, column=1, sticky="ew", padx=6, pady=6)
        drm_stereo_check = ttk.Checkbutton(
            stereo_frame,
            text="Stereo audio",
            variable=tx_drm_stereo_var,
            cursor="hand2",
            takefocus=True,
        )
        drm_stereo_check.pack(anchor="w", padx=6, pady=(8, 4))
        make_label(
            stereo_frame,
            "Stereo consumes more of the available audio capacity. Mono is usually clearer at the same low bitrate.",
            font=("Segoe UI", 9, "normal"),
            color=palette["mutedForeground"],
            
            wraplength=400,
        ).pack(anchor="w", padx=6, pady=(4, 0))

        def update_drm_metadata_visibility() -> None:
            if tx_mode_var.get().strip().upper() == "DRM":
                if not drm_metadata_card.winfo_manager():
                    drm_metadata_card.pack(fill="x", pady=(0, 12))
            else:
                drm_metadata_card.pack_forget()
        make_label(
            signal_card,
            "Signal settings",
            font=("Segoe UI", 15, "bold"),
            
        ).pack(anchor="w", pady=(0, 10))

        signal_fields = ttk.Frame(signal_card)
        signal_fields.pack(fill="x")
        signal_fields.grid_columnconfigure(0, weight=3, uniform="signal-fields")
        signal_fields.grid_columnconfigure(1, weight=2, uniform="signal-fields")

        mode_frame = ttk.Frame(signal_fields)
        mode_frame.grid(row=0, column=0, sticky="ew", padx=6, pady=6)
        make_label(
            mode_frame,
            "Signal type",
            font=("Segoe UI", 10, "bold"),
            color=palette["mutedForeground"],
            
        ).pack(anchor="w", pady=(0, 5))
        mode_picker = ttk.Combobox(
            mode_frame,
            textvariable=tx_mode_display_var,
            values=("AM", "USB", "LSB", "USB+C", "LSB+C", "DSB", "NFM", "RADE", "DRM [EXPERIMENTAL]", "CW"),
            state="readonly",
            height=8,
        )
        mode_picker.pack(fill="x", ipady=5)
        style_tx_combobox(mode_picker)

        mode_help = make_label(
            mode_frame,
            variable=tx_mode_help_var,
            font=("Segoe UI", 9, "normal"),
            color=palette["mutedForeground"],
            
            wraplength=570,
        )
        mode_help.pack(anchor="w", pady=(6, 0))

        def update_mode_help(_event=None) -> None:
            descriptions = {
                "AM": "AM - normal AM with carrier.",
                "USB": "USB - upper sideband.",
                "LSB": "LSB - lower sideband.",
                "USB+C": "USB+C - upper sideband with carrier.",
                "LSB+C": "LSB+C - lower sideband with carrier.",
                "DSB": "DSB - double-sideband suppressed carrier.",
                "NFM": (
                    "NFM - software-generated narrow FM with Standard (9 kHz) and Wide (11 kHz) profiles. "
                    "Wide uses a 6 kHz IF, 2.5 kHz peak deviation, and a 100-3,000 Hz voice band. "
                    "Both profiles use second-order Butterworth input filters and a 12 kHz TXITY transport."
                ),
                "RADE": (
                    "FreeDV RADE V1 - neural digital voice over TXITY's USB or LSB audio transport. "
                    "Uses a 1.5 kHz audio centre and channel. The first start downloads a verified native encoder package."
                ),
                "DRM": "DRM [EXPERIMENTAL] - one xHE-AAC or HE-AAC audio service. Choose 4.5 kHz robust, 9 kHz quality, or 9 kHz 64-QAM stereo maximum-quality profiles in DRM settings. Use a microphone or OGG, MP3, or FLAC playlists.",
                "CW": "CW - carrier only. MultiV sends the configured CW message instead of microphone or desktop audio.",
            }
            tx_mode_help_var.set(descriptions.get(tx_mode_var.get(), "Choose a signal mode."))
            update_drm_metadata_visibility()
            update_frequency_check()

        def restore_previous_mode() -> None:
            previous = str(drm_setup_state.get("previous_mode") or "AM")
            tx_mode_var.set(previous)
            tx_mode_display_var.set(
                "DRM [EXPERIMENTAL]" if previous == "DRM" else previous
            )
            update_mode_help()

        def set_mode_picker_enabled(enabled: bool) -> None:
            try:
                if mode_picker.winfo_exists():
                    mode_picker.configure(state="readonly" if enabled else "disabled")
            except tk.TclError:
                pass

        drm_progressbar: ttk.Progressbar | None = None

        def set_drm_progress_visible(visible: bool) -> None:
            progress = drm_progressbar
            if progress is None:
                return
            try:
                if not progress.winfo_exists():
                    return
                if visible:
                    progress.pack(fill="x", pady=(12, 0))
                    progress.start(25)
                else:
                    progress.stop()
                    progress.pack_forget()
            except tk.TclError:
                pass

        def start_drm_setup() -> None:
            drm_setup_state["running"] = True
            previous_mode = str(drm_setup_state.get("previous_mode") or "AM")
            tx_mode_var.set(previous_mode)
            tx_mode_display_var.set(
                "DRM [EXPERIMENTAL]" if previous_mode == "DRM" else previous_mode
            )
            tx_mode_help_var.set("Setting up DRM30. Downloading and building its encoder...")
            tx_status_var.set("Preparing DRM dependencies...")
            set_drm_progress_visible(True)
            set_mode_picker_enabled(False)

            def setup_worker() -> None:
                def report(line: str) -> None:
                    drm_setup_events.put(("log", line))

                try:
                    encoder = prepare_drm_encoder(on_log=report)
                except Exception as exc:
                    drm_setup_events.put(("error", str(exc)))
                else:
                    drm_setup_events.put(("success", str(encoder)))

            threading.Thread(
                target=setup_worker,
                daemon=True,
                name="txitpy-drm-setup",
            ).start()
            root.after(150, poll_drm_setup)


        def poll_drm_setup() -> None:
            completed = False
            while True:
                try:
                    kind, value = drm_setup_events.get_nowait()
                except queue.Empty:
                    break

                if kind == "log":
                    tx_status_var.set(value[:240])
                elif kind == "success":
                    completed = True
                    drm_setup_state["running"] = False
                    set_drm_progress_visible(False)
                    drm_setup_state["previous_mode"] = "DRM"
                    tx_mode_var.set("DRM")
                    tx_mode_display_var.set("DRM [EXPERIMENTAL]")
                    tx_status_var.set("DRM encoder is ready.")
                    update_mode_help()
                    set_mode_picker_enabled(True)
                elif kind == "error":
                    completed = True
                    drm_setup_state["running"] = False
                    set_drm_progress_visible(False)
                    restore_previous_mode()
                    tx_status_var.set("DRM setup failed.")
                    set_mode_picker_enabled(True)
                    messagebox.showerror(
                        "DRM setup failed",
                        value,
                        parent=root,
                    )

            if not completed and drm_setup_state["running"]:
                root.after(150, poll_drm_setup)

        def handle_mode_selection(_event=None) -> None:
            selected_label = tx_mode_display_var.get().strip().upper()
            selected = "DRM" if selected_label == "DRM [EXPERIMENTAL]" else selected_label
            tx_mode_var.set(selected)
            if selected != "DRM":
                if not drm_setup_state["running"]:
                    drm_setup_state["previous_mode"] = selected
                update_mode_help()
                return

            if drm_setup_state["running"]:
                return

            status = drm_setup_status()
            if status.get("ready"):
                drm_setup_state["previous_mode"] = "DRM"
                update_mode_help()
                return

            missing_build_tools = [
                str(item) for item in (status.get("missing_build_tools") or [])
            ]
            if missing_build_tools:
                from drm_support import VISUAL_STUDIO_BUILD_TOOLS_LABEL

                if (
                    os.name == "nt"
                    and missing_build_tools == [VISUAL_STUDIO_BUILD_TOOLS_LABEL]
                ):
                    install_prompt = (
                        "DRM needs Microsoft's Visual Studio C++ Build Tools.\n\n"
                        "MultiV can download the official installer and add the "
                        "'Desktop development with C++' workload, including the MSVC compiler "
                        "and Windows SDK. The installer may request administrator approval. "
                        "MultiV will then continue installing any missing DRM dependencies.\n\n"
                        "Install the C++ build tools and continue setting up DRM?"
                    )
                    if messagebox.askyesno(
                        "Install DRM build tools",
                        install_prompt,
                        parent=root,
                    ):
                        start_drm_setup()
                    else:
                        restore_previous_mode()
                    return

                restore_previous_mode()
                messagebox.showerror(
                    "DRM build tools missing",
                    "DRM needs these build tools, which MultiV could not detect:\n\n"
                    + "\n".join(missing_build_tools)
                    + "\n\nInstall the missing tools, then select DRM again.",
                    parent=root,
                )
                return

            missing = [str(item) for item in (status.get("missing") or [])]
            if not missing:
                missing = ["the DRM encoder has not been built on this computer"]

            prompt = (
                "DRM mode is not ready. MultiV could not detect:\n\n"
                + "\n".join(f"• {item}" for item in missing)
                + "\n\nDownload and install the missing components and build the DRM encoder now? "
                "MultiV will select DRM when setup finishes."
            )
            if not messagebox.askyesno("Set up DRM30", prompt, parent=root):
                restore_previous_mode()
                return

            start_drm_setup()

        mode_picker.bind("<<ComboboxSelected>>", handle_mode_selection)
        update_mode_help()
        if tx_mode_var.get().strip().upper() == "DRM":
            root.after_idle(handle_mode_selection)

        power_frame = ttk.Frame(signal_fields)
        power_frame.grid(row=0, column=1, sticky="nsew", padx=6, pady=6)
        make_label(
            power_frame,
            "Station power",
            font=("Segoe UI", 10, "bold"),
            color=palette["mutedForeground"],
            
        ).pack(anchor="w", pady=(0, 6))
        power_scale = ttk.Scale(
            power_frame,
            variable=tx_power_var,
            from_=-140,
            to=53,
            orient="horizontal",
            command=lambda value: tx_power_var.set(str(round(float(value)))),
            cursor="hand2",
            takefocus=True,
        )
        power_scale.pack(fill="x", expand=True, pady=(1, 0))
        make_label(
            power_frame,
            variable=tx_power_readout_var,
            font=("Segoe UI", 10, "bold"),
            color=palette["accent"],
            
        ).pack(anchor="e", pady=(3, 0))

        sharing = ttk.Checkbutton(
            signal_card,
            text="Allow shared frequency overlap with stations that also enable sharing",
            variable=tx_allow_overlap_var,
            command=update_frequency_check,
            cursor="hand2",
            takefocus=True,
        )
        sharing.pack(anchor="w", padx=6, pady=(8, 0))

        def toggle_advanced_settings() -> None:
            nonlocal advanced_expanded
            if advanced_expanded:
                advanced_container.pack_forget()
                advanced_toggle.configure(text="Show advanced settings")
                advanced_expanded = False
            else:
                advanced_container.pack(fill="x", before=cw_card, pady=(0, 12))
                advanced_toggle.configure(text="Hide advanced settings")
                advanced_expanded = True

        advanced_toggle = make_button(
            page,
            "Show advanced settings",
            toggle_advanced_settings,
            kind="tonal",
            padx=14,
            pady=8,
        )
        advanced_toggle.pack(anchor="w", pady=(0, 10))

        fade_card = make_card(advanced_container, padding=16)
        fade_card.pack(fill="x", pady=(0, 12))
        make_label(
            fade_card,
            "Signal fading",
            font=("Segoe UI", 15, "bold"),
            
        ).pack(anchor="w", pady=(0, 8))
        fade_enabled = ttk.Checkbutton(
            fade_card,
            text="Enable signal fading",
            variable=tx_fade_enabled_var,
            cursor="hand2",
            takefocus=True,
        )
        fade_enabled.pack(anchor="w", padx=6, pady=(0, 6))
        fade_fields = ttk.Frame(fade_card)
        fade_fields.pack(fill="x")
        fade_fields.grid_columnconfigure(0, weight=1, uniform="fade-fields")
        fade_fields.grid_columnconfigure(1, weight=1, uniform="fade-fields")
        fade_seconds_entry = add_tx_entry(
            fade_fields, "Fade cycle length (10-60 seconds)",
            tx_fade_seconds_var, column=0, row=0,
        )
        fade_min_entry = add_tx_entry(
            fade_fields, "Lowest power point (-140 to 50 dBm)",
            tx_fade_min_dbm_var, column=1, row=0,
        )
        fade_shape_frame = ttk.Frame(fade_fields)
        fade_shape_frame.grid(row=1, column=0, columnspan=2, sticky="ew", padx=6, pady=6)
        make_label(
            fade_shape_frame,
            "Fade style",
            font=("Segoe UI", 9, "bold"),
            color=palette["mutedForeground"],
            
        ).pack(anchor="w", pady=(0, 5))
        fade_shape_values = tuple(
            f"{shape_id} - {shape_name}" for shape_id, shape_name in fade_shape_names.items()
        )
        fade_shape_picker = ttk.Combobox(
            fade_shape_frame,
            textvariable=tx_fade_shape_var,
            values=fade_shape_values,
            state="readonly",
            height=6,
        )
        fade_shape_picker.pack(fill="x", ipady=5)
        style_tx_combobox(fade_shape_picker)
        fade_shape_help_var = tk.StringVar(root)
        def update_fade_shape_help(_event=None) -> None:
            selected_shape_id = tx_fade_shape_var.get().split("-", 1)[0].strip()
            fade_shape_help_var.set(fade_shape_names.get(selected_shape_id, "Choose a fade curve."))
        fade_shape_picker.bind("<<ComboboxSelected>>", update_fade_shape_help)
        update_fade_shape_help()
        make_label(
            fade_shape_frame,
            variable=fade_shape_help_var,
            font=("Segoe UI", 9, "normal"),
            color=palette["mutedForeground"],
            
        ).pack(anchor="w", pady=(5, 0))

        def update_fade_controls(*_args) -> None:
            state = "normal" if tx_fade_enabled_var.get() else "disabled"
            fade_seconds_entry.configure(state=state)
            fade_min_entry.configure(state=state)
            fade_shape_picker.configure(state="readonly" if state == "normal" else "disabled")
        tx_fade_enabled_var.trace_add("write", update_fade_controls)
        update_fade_controls()

        audio_filter_card = make_card(advanced_container, padding=16)
        audio_filter_card.pack(fill="x", pady=(0, 12))
        make_label(
            audio_filter_card,
            "Audio processing and filters",
            font=("Segoe UI", 15, "bold"),
            
        ).pack(anchor="w", pady=(0, 8))
        agc_row = ttk.Frame(audio_filter_card)
        agc_row.pack(fill="x", padx=6, pady=(0, 8))
        agc_toggle = ttk.Checkbutton(
            agc_row,
            text="Server AGC enabled",
            variable=tx_agc_enabled_var,
            cursor="hand2",
            takefocus=True,
        )
        agc_toggle.pack(side="left")
        audio_filter_grid = ttk.Frame(audio_filter_card)
        audio_filter_grid.pack(fill="x")
        for column in range(2):
            audio_filter_grid.grid_columnconfigure(column, weight=1, uniform="audio-filter-fields")

        def add_audio_scale(
            label: str,
            variable: tk.StringVar,
            minimum: float,
            maximum: float,
            step: float,
            formatter,
            row: int,
            column: int,
        ) -> ttk.Frame:
            wrapper = ttk.Frame(audio_filter_grid)
            wrapper.grid(row=row, column=column, sticky="ew", padx=6, pady=6)
            make_label(
                wrapper,
                label,
                font=("Segoe UI", 9, "bold"),
                color=palette["mutedForeground"],
                
            ).pack(anchor="w")
            value_var = tk.StringVar(root, "")
            def update_readout(*_args) -> None:
                try:
                    value_var.set(formatter(float(variable.get())))
                except (TypeError, ValueError):
                    value_var.set("Invalid value")
            variable.trace_add("write", update_readout)
            update_readout()

            def snap_scale(value: str) -> None:
                try:
                    snapped = minimum + round((float(value) - minimum) / step) * step
                    variable.set(f"{snapped:.12g}")
                except (TypeError, ValueError, ZeroDivisionError):
                    return

            scale = ttk.Scale(
                wrapper,
                variable=variable,
                from_=minimum,
                to=maximum,
                orient="horizontal",
                command=snap_scale,
                cursor="hand2",
                takefocus=True,
            )
            scale.pack(fill="x", pady=(1, 0))
            make_label(
                wrapper,
                variable=value_var,
                font=("Segoe UI", 9, "bold"),
                color=palette["accent"],
                
            ).pack(anchor="e")
            return wrapper

        add_audio_scale("AGC target level", tx_agc_target_var, 0.16, 0.24, 0.01,
                        lambda value: f"{value:.2f} RMS", 0, 0)
        add_audio_scale("Microphone gain", tx_mic_gain_var, -6, 6, 1,
                        lambda value: f"{value:+.0f} dB", 0, 1)
        add_audio_scale("Bass", tx_bass_var, -4, 4, 1,
                        lambda value: f"{value:+.0f} dB", 1, 0)
        add_audio_scale("Treble", tx_treble_var, -4, 4, 1,
                        lambda value: f"{value:+.0f} dB", 1, 1)
        add_audio_scale("Presence / clarity", tx_presence_var, -1, 1, 1,
                        lambda value: f"{value:+.0f} dB", 2, 0)
        add_audio_scale("Compression / drive", tx_drive_var, 0, 0.025, 0.005,
                        lambda value: f"{value * 100:.1f}%", 2, 1)
        add_audio_scale("SSB voice width", tx_ssb_bandwidth_var, 2400, 10000, 100,
                        lambda value: f"{value / 1000:.1f} kHz", 3, 0)
        add_audio_scale("SSB low cut", tx_ssb_low_cut_var, 0, 2000, 25,
                        lambda value: f"{value:.0f} Hz", 3, 1)
        add_audio_scale("SSB high edge cut", tx_ssb_high_cut_var, 0, 3000, 25,
                        lambda value: f"{value:.0f} Hz", 4, 0)
        add_audio_scale("AM / DSB bandwidth", tx_am_bandwidth_var, 3000, 12000, 500,
                        lambda value: f"{value / 1000:.1f} kHz", 4, 1)
        nfm_bandwidth_control = add_audio_scale(
            "NFM transport bandwidth",
            tx_nfm_transport_bandwidth_var,
            1_000,
            20_000,
            1_000,
            lambda value: f"{value / 1000:.0f} kHz",
            5,
            0,
        )
        nfm_profile_frame = ttk.Frame(audio_filter_card)
        nfm_profile_frame.pack(fill="x", padx=6, pady=6)
        make_label(
            nfm_profile_frame,
            "NFM audio profile",
            font=("Segoe UI", 9, "bold"),
            color=palette["mutedForeground"],
            
        ).pack(anchor="w", pady=(0, 4))
        nfm_profile_picker = ttk.Combobox(
            nfm_profile_frame,
            textvariable=tx_nfm_profile_var,
            values=NFM_PROFILES,
            state="readonly",
            height=4,
        )
        nfm_profile_picker.pack(fill="x", ipady=5)
        style_tx_combobox(nfm_profile_picker)
        make_label(
            nfm_profile_frame,
            "Wide opens the voice band to 3 kHz and increases the estimated occupied bandwidth to 11 kHz.",
            font=("Segoe UI", 9, "normal"),
            color=palette["mutedForeground"],
            
            wraplength=400,
        ).pack(anchor="w", pady=(4, 0))

        def update_nfm_bandwidth_visibility(*_args) -> None:
            if tx_mode_var.get().strip().upper() == "NFM":
                nfm_bandwidth_control.grid()
                nfm_profile_frame.pack(fill="x", padx=6, pady=6)
            else:
                nfm_bandwidth_control.grid_remove()
                nfm_profile_frame.pack_forget()

        tx_mode_var.trace_add("write", update_nfm_bandwidth_visibility)
        update_nfm_bandwidth_visibility()

        am_pre_emphasis = ttk.Checkbutton(
            audio_filter_card,
            text="AM pre-emphasis (brighter audio)",
            variable=tx_am_pre_emphasis_var,
            cursor="hand2",
            takefocus=True,
        )
        am_pre_emphasis.pack(anchor="w", padx=6, pady=(7, 0))
        make_label(
            audio_filter_card,
            "Changes apply automatically during transmission. The signal briefly stops while MultiV rebuilds the active audio path and starts again.",
            font=("Segoe UI", 9, "normal"),
            color=palette["mutedForeground"],
            
            wraplength=850,
        ).pack(anchor="w", padx=6, pady=(7, 0))

        audio_card = make_card(page, padding=16)
        audio_card.pack(fill="x", pady=(0, 12))
        icecast_card = make_card(page, padding=16)
        icecast_card.pack(fill="x", pady=(0, 12))
        make_label(
            icecast_card,
            "Output destinations",
            font=("Segoe UI", 15, "bold"),
        ).pack(anchor="w", pady=(0, 4))
        make_label(
            icecast_card,
            "Choose which providers receive the selected audio. Each provider can be opened, disabled, and reordered independently.",
            font=("Segoe UI", 9, "normal"),
            color=palette["mutedForeground"],
            wraplength=850,
        ).pack(anchor="w", pady=(0, 10))

        output_rows_container = ttk.Frame(icecast_card)
        output_rows_container.pack(fill="x")
        output_row_widgets: dict[str, ttk.LabelFrame] = {}
        output_row_bodies: dict[str, ttk.Frame] = {}
        output_row_title_buttons: dict[str, ttk.Button] = {}
        output_row_menus: dict[str, tk.Menu] = {}
        output_row_expanded = {"txity": True, "icecast": True}

        def refresh_output_order() -> None:
            for provider_name in output_order:
                output_row_widgets[provider_name].pack_forget()
            for provider_name in output_order:
                output_row_widgets[provider_name].pack(fill="x", pady=(0, 8))
            for provider_name, menu in output_row_menus.items():
                menu.entryconfigure(
                    0,
                    state="disabled" if output_order.index(provider_name) == 0 else "normal",
                )
                menu.entryconfigure(
                    1,
                    state=(
                        "disabled"
                        if output_order.index(provider_name) == len(output_order) - 1
                        else "normal"
                    ),
                )

        def move_output_provider(provider_name: str, offset: int) -> None:
            current_index = output_order.index(provider_name)
            target_index = current_index + offset
            if not 0 <= target_index < len(output_order):
                return
            output_order[current_index], output_order[target_index] = (
                output_order[target_index],
                output_order[current_index],
            )
            refresh_output_order()
            schedule_tx_settings_save()
            schedule_live_reconfigure()

        def toggle_output_provider_body(provider_name: str) -> None:
            body = output_row_bodies[provider_name]
            expanded = output_row_expanded[provider_name]
            if expanded:
                body.pack_forget()
                output_row_expanded[provider_name] = False
            else:
                body.pack(fill="x", padx=6, pady=(4, 2))
                output_row_expanded[provider_name] = True
            output_row_title_buttons[provider_name].configure(
                text=(
                    ("TXITY" if provider_name == "txity" else "Icecast")
                    + ("  \u25be" if output_row_expanded[provider_name] else "  \u25b8")
                )
            )

        def provider_enabled_changed(*_args) -> None:
            schedule_tx_settings_save()
            schedule_live_reconfigure()

        def create_output_provider_row(
            provider_name: str,
            title: str,
            enabled_variable: tk.BooleanVar,
        ) -> ttk.Frame:
            row = ttk.LabelFrame(output_rows_container, padding=8)
            header = ttk.Frame(row)
            header.pack(fill="x")
            menu_button = ttk.Menubutton(header, text="\u2261", direction="below")
            menu = tk.Menu(menu_button, tearoff=False)
            menu.add_command(
                label="Move up",
                command=lambda: move_output_provider(provider_name, -1),
            )
            menu.add_command(
                label="Move down",
                command=lambda: move_output_provider(provider_name, 1),
            )
            menu_button.configure(menu=menu)
            menu_button.pack(side="left", padx=(0, 8))
            output_row_menus[provider_name] = menu

            title_button = ttk.Button(
                header,
                text=title + "  \u25be",
                command=lambda: toggle_output_provider_body(provider_name),
            )
            title_button.pack(side="left", fill="x", expand=True)
            output_row_title_buttons[provider_name] = title_button
            ttk.Checkbutton(
                header,
                text="Enabled",
                variable=enabled_variable,
            ).pack(side="right", padx=(10, 2))
            enabled_variable.trace_add("write", provider_enabled_changed)

            body = ttk.Frame(row, padding=(6, 4, 6, 2))
            body.pack(fill="x", padx=6, pady=(4, 2))
            output_row_widgets[provider_name] = row
            output_row_bodies[provider_name] = body
            return body

        txity_output_body = create_output_provider_row(
            "txity",
            "TXITY",
            txity_output_enabled_var,
        )
        make_label(
            txity_output_body,
            "Sends the selected audio to the TXITY virtual transmitter using your signed-in TXITY session.",
            font=("Segoe UI", 9, "normal"),
            color=palette["mutedForeground"],
            wraplength=850,
        ).pack(anchor="w", pady=(0, 4))

        icecast_output_body = create_output_provider_row(
            "icecast",
            "Icecast",
            icecast_output_enabled_var,
        )
        refresh_output_order()

        built_in_server_frame = ttk.LabelFrame(
            icecast_output_body,
            padding=10,
            text="Built-in Icecast server",
        )
        built_in_server_frame.pack(fill="x", pady=(0, 12))
        ttk.Checkbutton(
            built_in_server_frame,
            text="Host a stream from MultiV",
            variable=tx_icecast_server_enabled_var,
        ).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 7))

        def add_server_setting(label_text: str, variable: tk.StringVar, row: int, *, secret: bool = False, width: int = 18) -> None:
            make_label(
                built_in_server_frame,
                label_text,
                font=("Segoe UI", 9, "normal"),
                color=palette["mutedForeground"],
                
            ).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=4)
            entry = ttk.Entry(
                built_in_server_frame,
                textvariable=variable,
                show="*" if secret else "",
                width=width,
                font=("Segoe UI", 9),
            )
            entry.grid(row=row, column=1, sticky="ew", ipady=4, padx=(0, 16), pady=4)
            return entry

        built_in_server_frame.grid_columnconfigure(1, weight=1)
        add_server_setting("Port", tx_icecast_server_port_var, 1, width=8)
        add_server_setting("Mountpoint", tx_icecast_server_mount_var, 2)
        add_server_setting("Listener password (optional)", tx_icecast_server_password_var, 3, secret=True)
        add_server_setting("Maximum listeners", tx_icecast_server_max_listeners_var, 4, width=8)

        available_server_formats = available_icecast_formats()
        preferred_server_formats = ("ogg_opus", "ogg_vorbis", "aac_plus", "ogg_flac", "mp3")
        selected_server_format = tx_icecast_server_format_var.get()
        if selected_server_format not in available_server_formats:
            selected_server_format = next(
                (item for item in preferred_server_formats if item in available_server_formats),
                next(iter(available_server_formats), "mp3"),
            )
            tx_icecast_server_format_var.set(selected_server_format)
        server_format_label_to_key = {
            label: key for key, label in available_server_formats.items()
        }
        server_format_label_var = tk.StringVar(
            root,
            available_server_formats.get(selected_server_format, "No encoders available"),
        )
        make_label(
            built_in_server_frame,
            "Audio format",
            font=("Segoe UI", 9, "normal"),
            color=palette["mutedForeground"],
        ).grid(row=5, column=0, sticky="w", padx=(0, 8), pady=4)
        server_format_picker = ttk.Combobox(
            built_in_server_frame,
            textvariable=server_format_label_var,
            values=tuple(available_server_formats.values()),
            state="readonly" if available_server_formats else "disabled",
            width=28,
        )
        server_format_picker.grid(row=5, column=1, sticky="ew", padx=(0, 16), pady=4)
        style_tx_combobox(server_format_picker)

        make_label(
            built_in_server_frame,
            "Bitrate (kbps)",
            font=("Segoe UI", 9, "normal"),
            color=palette["mutedForeground"],
        ).grid(row=6, column=0, sticky="w", padx=(0, 8), pady=4)
        server_bitrate_picker = ttk.Combobox(
            built_in_server_frame,
            textvariable=tx_icecast_bitrate_var,
            values=tuple(str(value) for value in ICECAST_BITRATES),
            state="readonly",
            width=10,
        )
        server_bitrate_picker.grid(row=6, column=1, sticky="w", padx=(0, 16), pady=4)
        style_tx_combobox(server_bitrate_picker)

        def update_builtin_server_format(*_args) -> None:
            format_key = server_format_label_to_key.get(server_format_label_var.get())
            if format_key is None:
                return
            tx_icecast_server_format_var.set(format_key)
            if format_key == "ogg_flac":
                tx_icecast_bitrate_var.set("0")
                server_bitrate_picker.configure(state="disabled")
            else:
                if tx_icecast_bitrate_var.get() not in {str(value) for value in ICECAST_BITRATES}:
                    tx_icecast_bitrate_var.set("96")
                if format_key == "aac_plus" and int(tx_icecast_bitrate_var.get()) > 128:
                    tx_icecast_bitrate_var.set("128")
                elif format_key == "ogg_opus" and int(tx_icecast_bitrate_var.get()) < 48:
                    tx_icecast_bitrate_var.set("48")
                server_bitrate_picker.configure(state="readonly")
            schedule_tx_settings_save()
            schedule_live_reconfigure()

        server_format_label_var.trace_add("write", update_builtin_server_format)
        update_builtin_server_format()

        make_label(
            built_in_server_frame,
            "The server starts with transmission and stops when transmission stops. Stream URL: http://YOUR-IP:PORT/MOUNT. The mountpoint can use any extension. Router/firewall access may be needed for remote listeners.",
            font=("Segoe UI", 9, "normal"),
            color=palette["mutedForeground"],
            wraplength=850,
        ).grid(row=7, column=0, columnspan=4, sticky="w", pady=(5, 0))

        make_label(
            icecast_output_body,
            "Remote Icecast servers",
            font=("Segoe UI", 11, "bold"),
            
        ).pack(anchor="w", pady=(0, 6))

        icecast_list_frame = ttk.Frame(
            icecast_output_body,
        )
        icecast_list_frame.pack(fill="x")
        icecast_listbox = tk.Listbox(
            icecast_list_frame,
            height=4,
            exportselection=False,
            activestyle="none",
            bg=palette["background"],
            fg=palette["foreground"],
            selectbackground=palette["selectionBackground"],
            selectforeground=palette["selectionForeground"],
            highlightthickness=1,
            highlightbackground=palette["outline"],
            highlightcolor=palette["accent"],
            relief="flat",
            borderwidth=1,
            font=("Segoe UI", 9),
            takefocus=True,
        )
        icecast_listbox.pack(fill="x", expand=True)

        def refresh_icecast_list() -> None:
            try:
                icecast_listbox.delete(0, tk.END)
                for destination in icecast_destinations:
                    url = str(destination.get("url") or "").strip()
                    try:
                        name = normalize_icecast_destination(destination)["name"]
                    except ValueError:
                        name = url or "Incomplete destination"
                    enabled = bool(destination.get("enabled", True))
                    state = tx_icecast_states.get(name, "Not connected")
                    audio_format = str(destination.get("format") or "mp3")
                    format_label = ICECAST_FORMATS.get(audio_format, {}).get("label", "MP3")
                    bitrate = str(destination.get("bitrate") or "128")
                    detail = "lossless" if audio_format == "ogg_flac" else f"{bitrate} kbps"
                    icecast_listbox.insert(
                        tk.END,
                        f'{"Enabled" if enabled else "Disabled"}  |  {name}  |  {format_label} {detail}  |  {state}',
                    )
                if not icecast_destinations:
                    icecast_listbox.insert(tk.END, "No destinations configured")
            except tk.TclError:
                return
            enabled_count = sum(
                1 for item in icecast_destinations
                if bool(item.get("enabled", True))
            )
            if enabled_count:
                tx_icecast_status_var.set(
                    f"{enabled_count} Icecast destination(s) enabled. They connect when transmission starts."
                )
            else:
                tx_icecast_status_var.set("No Icecast destinations enabled.")

        def edit_icecast_destination(index: int | None = None) -> None:
            existing = (
                dict(icecast_destinations[index])
                if index is not None and 0 <= index < len(icecast_destinations)
                else {}
            )
            dialog = tk.Toplevel(root)
            dialog.title("Edit Icecast destination" if existing else "Add Icecast destination")
            dialog.transient(root)
            dialog.resizable(False, False)
            dialog.configure(background=palette["background"])
            dialog.grab_set()

            fields = ttk.Frame(dialog, padding=(18, 16))
            fields.pack(fill="both", expand=True)
            fields.grid_columnconfigure(0, weight=1)
            url_var = tk.StringVar(dialog, str(existing.get("url") or ""))
            username_var = tk.StringVar(dialog, str(existing.get("username") or "source"))
            password_var = tk.StringVar(dialog, str(existing.get("password") or ""))
            enabled_var = tk.BooleanVar(dialog, bool(existing.get("enabled", True)))
            available_formats = available_icecast_formats()
            format_labels = {label: key for key, label in available_formats.items()}
            selected_format = str(existing.get("format") or "ogg_opus")
            if selected_format not in available_formats:
                selected_format = next(
                    (item for item in ("ogg_opus", "ogg_vorbis", "aac_plus", "ogg_flac", "mp3") if item in available_formats),
                    "mp3",
                )
            format_var = tk.StringVar(dialog, available_formats.get(selected_format, "MP3"))
            bitrate_var = tk.StringVar(dialog, str(existing.get("bitrate") or "96"))

            def add_dialog_field(label_text: str, variable: tk.StringVar, row: int, *, secret: bool = False) -> ttk.Entry:
                make_label(
                    fields,
                    label_text,
                    font=("Segoe UI", 9, "bold"),
                    color=palette["mutedForeground"],
                    
                ).grid(row=row * 2, column=0, sticky="w", pady=(8 if row else 0, 4))
                entry = ttk.Entry(
                    fields,
                    textvariable=variable,
                    show="*" if secret else "",
                    font=("Segoe UI", 10),
                    width=54,
                )
                entry.grid(row=row * 2 + 1, column=0, sticky="ew", ipady=6)
                return entry

            add_dialog_field("Server URL including mountpoint", url_var, 0)
            add_dialog_field("Source username", username_var, 1)
            password_entry = add_dialog_field("Source password", password_var, 2, secret=True)

            make_label(
                fields,
                "Audio format",
                font=("Segoe UI", 9, "bold"),
                color=palette["mutedForeground"],
                
            ).grid(row=6, column=0, sticky="w", pady=(8, 4))
            format_picker = ttk.Combobox(
                fields,
                textvariable=format_var,
                values=tuple(available_formats.values()),
                state="readonly",
                width=50,
            )
            format_picker.grid(row=7, column=0, sticky="ew")
            style_tx_combobox(format_picker)

            make_label(
                fields,
                "Bitrate (kbps)",
                font=("Segoe UI", 9, "bold"),
                color=palette["mutedForeground"],
                
            ).grid(row=8, column=0, sticky="w", pady=(8, 4))
            bitrate_picker = ttk.Combobox(
                fields,
                textvariable=bitrate_var,
                values=tuple(str(value) for value in ICECAST_BITRATES),
                state="readonly",
                width=50,
            )
            bitrate_picker.grid(row=9, column=0, sticky="ew")
            style_tx_combobox(bitrate_picker)

            def update_format_controls(*_args) -> None:
                format_key = format_labels.get(format_var.get(), "mp3")
                if format_key == "ogg_flac":
                    bitrate_var.set("0")
                    bitrate_picker.configure(state="disabled")
                else:
                    if bitrate_var.get() not in {str(value) for value in ICECAST_BITRATES}:
                        bitrate_var.set("128")
                    bitrate_picker.configure(state="readonly")
                if format_key == "aac_plus" and int(bitrate_var.get() or 128) > 128:
                    bitrate_var.set("128")
                elif format_key == "ogg_opus" and int(bitrate_var.get() or 128) < 48:
                    bitrate_var.set("48")

            format_var.trace_add("write", update_format_controls)
            update_format_controls()

            ttk.Checkbutton(
                fields,
                text="Enable this destination",
                variable=enabled_var,
            ).grid(row=10, column=0, sticky="w", pady=(12, 4))

            make_label(
                fields,
                "Use http:// or https:// and include a mountpoint, such as /live.mp3. Credentials are sent using HTTP Basic authentication.",
                font=("Segoe UI", 9, "normal"),
                color=palette["mutedForeground"],
                
                wraplength=430,
            ).grid(row=11, column=0, sticky="w", pady=(4, 12))

            def save_destination() -> None:
                candidate = {
                    "url": url_var.get().strip(),
                    "username": username_var.get().strip() or "source",
                    "password": password_var.get(),
                    "enabled": bool(enabled_var.get()),
                    "format": format_labels.get(format_var.get(), "mp3"),
                    "bitrate": bitrate_var.get(),
                }
                if candidate["enabled"]:
                    try:
                        normalized = normalize_icecast_destination(candidate)
                    except ValueError as exc:
                        messagebox.showerror("Invalid Icecast destination", str(exc), parent=dialog)
                        return
                    duplicate = next(
                        (
                            position
                            for position, item in enumerate(icecast_destinations)
                            if position != index
                            and str(item.get("url") or "").strip().casefold()
                            == candidate["url"].casefold()
                        ),
                        None,
                    )
                    if duplicate is not None:
                        messagebox.showerror(
                            "Duplicate Icecast destination",
                            "That server URL is already configured.",
                            parent=dialog,
                        )
                        return
                elif candidate["url"]:
                    try:
                        normalize_icecast_destination(candidate)
                    except ValueError:
                        pass
                if index is None:
                    if len(icecast_destinations) >= 8:
                        messagebox.showerror(
                            "Destination limit",
                            "MultiV supports up to eight Icecast destinations.",
                            parent=dialog,
                        )
                        return
                    icecast_destinations.append(candidate)
                else:
                    icecast_destinations[index] = candidate
                schedule_tx_settings_save()
                refresh_icecast_list()
                schedule_live_reconfigure()
                dialog.destroy()

            actions = ttk.Frame(fields)
            actions.grid(row=12, column=0, sticky="e")
            make_button(
                actions,
                "Cancel",
                dialog.destroy,
                kind="text",
                padx=12,
                pady=7,
            ).pack(side="right", padx=(8, 0))
            make_button(
                actions,
                "Save destination",
                save_destination,
                kind="primary",
                padx=14,
                pady=7,
            ).pack(side="right")
            dialog.bind("<Return>", lambda _event: save_destination())
            dialog.bind("<Escape>", lambda _event: dialog.destroy())
            dialog.update_idletasks()
            dialog.geometry(
                f"+{root.winfo_rootx() + 80}+{root.winfo_rooty() + 80}"
            )
            password_entry.focus_set()

        def selected_icecast_index() -> int | None:
            selection = icecast_listbox.curselection()
            if not selection or selection[0] >= len(icecast_destinations):
                return None
            return int(selection[0])

        def add_icecast_destination() -> None:
            if len(icecast_destinations) >= 8:
                messagebox.showinfo(
                    "Icecast destination limit",
                    "MultiV supports up to eight Icecast destinations.",
                    parent=root,
                )
                return
            edit_icecast_destination()

        def edit_selected_icecast_destination() -> None:
            index = selected_icecast_index()
            if index is not None:
                edit_icecast_destination(index)

        def remove_selected_icecast_destination() -> None:
            index = selected_icecast_index()
            if index is None:
                return
            del icecast_destinations[index]
            schedule_tx_settings_save()
            refresh_icecast_list()
            schedule_live_reconfigure()

        icecast_actions = ttk.Frame(
            icecast_output_body,
        )
        icecast_actions.pack(fill="x", pady=(8, 0))
        make_button(
            icecast_actions,
            "Add server",
            add_icecast_destination,
            kind="tonal",
            padx=12,
            pady=7,
        ).pack(side="left")
        make_button(
            icecast_actions,
            "Edit",
            edit_selected_icecast_destination,
            kind="text",
            padx=10,
            pady=7,
        ).pack(side="left", padx=(4, 0))
        make_button(
            icecast_actions,
            "Remove",
            remove_selected_icecast_destination,
            kind="text",
            padx=10,
            pady=7,
        ).pack(side="left", padx=(4, 0))

        make_label(
            icecast_output_body,
            variable=tx_icecast_status_var,
            font=("Segoe UI", 9, "normal"),
            color=palette["mutedForeground"],
            
            wraplength=850,
        ).pack(anchor="w", pady=(8, 0))
        refresh_icecast_list()

        make_label(
            audio_card,
            "Audio input",
            font=("Segoe UI", 15, "bold"),
            
        ).pack(anchor="w", pady=(0, 10))

        audio_fields = ttk.Frame(audio_card)
        audio_fields.pack(fill="x")
        audio_fields.grid_columnconfigure(0, weight=1, uniform="audio-fields")
        audio_fields.grid_columnconfigure(1, weight=1, uniform="audio-fields")

        source_frame = ttk.Frame(audio_fields)
        source_frame.grid(row=0, column=0, sticky="ew", padx=6, pady=6)
        make_label(
            source_frame,
            "Input method",
            font=("Segoe UI", 9, "bold"),
            color=palette["mutedForeground"],
            
        ).pack(anchor="w", pady=(0, 5))
        source_picker = ttk.Combobox(
            source_frame,
            textvariable=tx_audio_source_var,
            values=(
                "Microphone / line input",
                "Desktop audio (output loopback)",
                "Audio playlist",
            ),
            state="readonly",
            height=4,
        )
        source_picker.pack(fill="x", ipady=5)
        style_tx_combobox(source_picker)

        device_frame = ttk.Frame(audio_fields)
        device_frame.grid(row=0, column=1, sticky="ew", padx=6, pady=6)
        make_label(
            device_frame,
            variable=tx_audio_device_label_var,
            font=("Segoe UI", 9, "bold"),
            color=palette["mutedForeground"],
            
        ).pack(anchor="w", pady=(0, 5))
        device_picker = ttk.Combobox(
            device_frame,
            textvariable=tx_audio_device_var,
            values=("System default",),
            state="readonly",
            height=8,
        )
        device_picker.pack(fill="x", ipady=5)
        style_tx_combobox(device_picker)

        playlist_frame = ttk.Frame(audio_fields)
        playlist_frame.grid(
            row=1,
            column=0,
            columnspan=2,
            sticky="ew",
            padx=6,
            pady=6,
        )
        make_label(
            playlist_frame,
            "Playlist source",
            font=("Segoe UI", 10, "bold"),
            
        ).pack(anchor="w", pady=(0, 5))
        playlist_count_var = tk.StringVar(root, f"{len(tx_playlist_files)} tracks")
        make_label(
            playlist_frame,
            variable=playlist_count_var,
            font=("Segoe UI", 9, "normal"),
            color=palette["mutedForeground"],
            
        ).pack(anchor="w", pady=(0, 4))

        playlist_list_frame = ttk.Frame(playlist_frame)
        playlist_list_frame.pack(fill="x", pady=(0, 7))
        playlist_listbox = tk.Listbox(
            playlist_list_frame,
            height=7,
            selectmode="extended",
            exportselection=False,
            activestyle="none",
            bg=palette["background"],
            fg=palette["foreground"],
            selectbackground=palette["selectionBackground"],
            selectforeground=palette["selectionForeground"],
            highlightthickness=2,
            highlightbackground=palette["outlineVariant"],
            highlightcolor=palette["accent"],
            relief="flat",
            borderwidth=1,
            font=("Segoe UI", 9),
            takefocus=True,
        )
        playlist_listbox.pack(side="left", fill="both", expand=True)
        playlist_scrollbar = ttk.Scrollbar(
            playlist_list_frame,
            orient="vertical",
            command=playlist_listbox.yview,
        )
        playlist_scrollbar.pack(side="right", fill="y")
        playlist_listbox.configure(yscrollcommand=playlist_scrollbar.set)

        playlist_display_paths: list[str] = []

        def update_playlist_list() -> None:
            selected_paths = {
                playlist_display_paths[index].casefold()
                for index in playlist_listbox.curselection()
                if 0 <= index < len(playlist_display_paths)
            }
            try:
                playlist_listbox.delete(0, tk.END)
                playlist_display_paths.clear()
                name_counts: dict[str, int] = {}
                for file_path in tx_playlist_files:
                    base_name = Path(file_path).name
                    name_counts[base_name.casefold()] = name_counts.get(base_name.casefold(), 0) + 1
                for index, file_path in enumerate(tx_playlist_files):
                    playlist_display_paths.append(file_path)
                    file_path_obj = Path(file_path)
                    display = file_path_obj.name
                    if name_counts.get(display.casefold(), 0) > 1:
                        display = f"{display}  ({file_path_obj.parent.name})"
                    playlist_listbox.insert(tk.END, display)
                    if file_path.casefold() in selected_paths:
                        playlist_listbox.selection_set(index)
                playlist_count_var.set(
                    f"{len(tx_playlist_files)} track" + ("" if len(tx_playlist_files) == 1 else "s")
                )
            except tk.TclError:
                return

        playlist_ui_refresh_callback = update_playlist_list

        def canonical_playlist_path(value: str | Path) -> str:
            return str(Path(value).expanduser().resolve(strict=False)).casefold()

        def playlist_path_is_within(candidate: str | Path, folder: str | Path) -> bool:
            candidate_path = canonical_playlist_path(candidate)
            folder_path = canonical_playlist_path(folder)
            try:
                return os.path.commonpath((candidate_path, folder_path)) == folder_path
            except ValueError:
                return False

        def add_playlist_sources(sources: list[str] | tuple[str, ...]) -> None:
            normalized_sources = normalize_audio_sources(sources)
            if not normalized_sources:
                tx_playlist_status_var.set("No supported audio files or folders were found.")
                return

            added_sources: list[str] = []
            exclusions_cleared = False
            with playlist_watch_lock:
                existing_sources = {canonical_playlist_path(item) for item in tx_playlist_sources}
                for source in normalized_sources:
                    identity = canonical_playlist_path(source)
                    source_path = Path(source)
                    if identity not in existing_sources:
                        tx_playlist_sources.append(source)
                        existing_sources.add(identity)
                        added_sources.append(source)

                    if source_path.is_dir():
                        exclusions_to_clear = [
                            excluded for excluded in tx_playlist_excluded_files
                            if playlist_path_is_within(excluded, source_path)
                        ]
                        if exclusions_to_clear:
                            tx_playlist_excluded_files.difference_update(exclusions_to_clear)
                            exclusions_cleared = True
                    elif identity in tx_playlist_excluded_files:
                        tx_playlist_excluded_files.discard(identity)
                        exclusions_cleared = True

            if added_sources or exclusions_cleared:
                added, _removed = refresh_playlist_from_sources()
                if added:
                    tx_playlist_status_var.set(f"Added {added} track(s).")
                elif exclusions_cleared:
                    tx_playlist_status_var.set("Playlist entries restored.")
                elif any(Path(item).is_dir() for item in added_sources):
                    tx_playlist_status_var.set(
                        "Folder added. MultiV will refresh the playlist when its contents change."
                    )
                else:
                    tx_playlist_status_var.set("Those audio files are already in the playlist.")
            else:
                tx_playlist_status_var.set("Those files and folders are already in the playlist.")

        def add_playlist_files() -> None:
            selected = filedialog.askopenfilenames(
                parent=root,
                title="Add audio files",
                filetypes=(
                    ("Audio files", "*.wav *.flac *.ogg *.aiff *.aif *.mp3 *.m4a *.opus"),
                    ("All files", "*.*"),
                ),
            )
            if selected:
                add_playlist_sources(selected)

        def add_playlist_folder() -> None:
            selected = filedialog.askdirectory(
                parent=root,
                title="Add audio folder",
                mustexist=True,
            )
            if selected:
                add_playlist_sources([selected])

        def remove_playlist_selection() -> None:
            selected = sorted(playlist_listbox.curselection())
            if not selected:
                return
            removed_paths = [
                tx_playlist_files[index]
                for index in selected
                if 0 <= index < len(tx_playlist_files)
            ]
            with playlist_watch_lock:
                for file_path in removed_paths:
                    identity = canonical_playlist_path(file_path)
                    tx_playlist_excluded_files.add(identity)
                    tx_playlist_sources[:] = [
                        source for source in tx_playlist_sources
                        if canonical_playlist_path(source) != identity
                    ]
            refresh_playlist_from_sources()
            tx_playlist_status_var.set(f"Removed {len(removed_paths)} track(s).")

        def clear_playlist() -> None:
            with playlist_watch_lock:
                tx_playlist_sources.clear()
                tx_playlist_excluded_files.clear()
            refresh_playlist_from_sources()
            tx_playlist_status_var.set("Playlist cleared.")

        def refresh_playlist_manually() -> None:
            added, removed = refresh_playlist_from_sources()
            tx_playlist_status_var.set(
                f"Playlist refreshed: {added} added, {removed} removed."
            )

        playlist_actions = ttk.Frame(playlist_frame)
        playlist_actions.pack(fill="x", pady=(0, 6))
        make_button(
            playlist_actions,
            "Add files",
            add_playlist_files,
            kind="tonal",
            padx=12,
            pady=7,
        ).pack(side="left", padx=(0, 6))
        make_button(
            playlist_actions,
            "Add folder",
            add_playlist_folder,
            kind="tonal",
            padx=12,
            pady=7,
        ).pack(side="left", padx=(0, 6))
        make_button(
            playlist_actions,
            "Remove selected",
            remove_playlist_selection,
            kind="tonal",
            padx=12,
            pady=7,
        ).pack(side="left", padx=(0, 6))
        make_button(
            playlist_actions,
            "Clear",
            clear_playlist,
            kind="text",
            padx=10,
            pady=7,
        ).pack(side="left")
        make_button(
            playlist_actions,
            "Refresh",
            refresh_playlist_manually,
            kind="text",
            padx=10,
            pady=7,
        ).pack(side="left")

        playlist_options = ttk.Frame(playlist_frame)
        playlist_options.pack(fill="x", pady=(2, 4))
        for label, variable in (
            ("Shuffle playlist", tx_playlist_shuffle_var),
            ("Loop playlist", tx_playlist_loop_var),
        ):
            option = ttk.Checkbutton(
                playlist_options,
                text=label,
                variable=variable,
                cursor="hand2",
                takefocus=True,
            )
            option.pack(side="left", padx=(0, 18))

        make_label(
            playlist_frame,
            variable=tx_playlist_status_var,
            font=("Segoe UI", 9, "normal"),
            color=palette["mutedForeground"],
            
            wraplength=850,
        ).pack(anchor="w", pady=(3, 0))
        make_label(
            playlist_frame,
            "Supported files: WAV, FLAC, OGG, AIFF, MP3, M4A, and Opus. MultiV refreshes added folders when their contents change.",
            font=("Segoe UI", 8, "normal"),
            color=palette["mutedForeground"],
            
            wraplength=850,
        ).pack(anchor="w", pady=(4, 0))
        playlist_listbox.bind(
            "<Delete>",
            lambda _event: remove_playlist_selection(),
        )
        update_playlist_list()

        def refresh_audio_devices(_event=None) -> None:
            tx_audio_device_ids.clear()
            selected_source = tx_audio_source_var.get()
            if selected_source == "Audio playlist":
                return
            if selected_source == "Desktop audio (output loopback)":
                tx_audio_device_label_var.set(
                    "Desktop monitor source"
                    if sys.platform.startswith("linux")
                    else "Desktop output device"
                )
                try:
                    devices = list_loopback_devices()
                except Exception as exc:
                    devices = []
                    details = safe_error_text(str(exc), 300)
                    tx_status_var.set(details)
                values = []
                for device_id, device_name in devices:
                    tx_audio_device_ids[device_name] = device_id
                    values.append(device_name)
                if not values:
                    values = ["No desktop loopback devices found"]
                    tx_audio_device_var.set(values[0])
                    device_picker.configure(values=values)
                    return
            else:
                tx_audio_device_label_var.set("Microphone / line input")
                tx_audio_device_ids["System default"] = None
                values = ["System default"]
                try:
                    for device_id, device_name in list_audio_inputs():
                        display = f"{device_id}: {device_name}"
                        tx_audio_device_ids[display] = device_id
                        values.append(display)
                except Exception as exc:
                    details = safe_error_text(str(exc), 300)
                    tx_status_var.set(details)

            device_picker.configure(values=values)
            if tx_audio_device_var.get() not in tx_audio_device_ids:
                tx_audio_device_var.set(values[0])

        source_picker.bind("<<ComboboxSelected>>", lambda _event: update_audio_source_controls())
        refresh_button = make_button(
            audio_card,
            "Refresh audio devices",
            refresh_audio_devices,
            kind="tonal",
            padx=12,
            pady=7,
        )
        refresh_button.pack(anchor="w", padx=6, pady=(8, 2))

        def update_audio_source_controls() -> None:
            base_sources = (
                "Microphone / line input",
                "Desktop audio (output loopback)",
                "Audio playlist",
            )
            source_values = base_sources
            source_picker.configure(values=source_values)
            if tx_audio_source_var.get() not in source_values:
                tx_audio_source_var.set("Microphone / line input")

            selected_source = tx_audio_source_var.get()
            if selected_source == "Audio playlist":
                device_frame.grid_remove()
                playlist_frame.grid()
                refresh_button.pack_forget()
            else:
                playlist_frame.grid_remove()
                device_frame.grid()
                if refresh_button.winfo_manager() != "pack":
                    refresh_button.pack(anchor="w", padx=6, pady=(8, 2))
                refresh_audio_devices()

        tx_mode_var.trace_add("write", lambda *_args: update_audio_source_controls())
        update_audio_source_controls()

        make_label(
            audio_card,
            "Choose a microphone, desktop output, or playlist. DRM mode supports microphone and playlist input. "
            "CW mode uses its configured message instead of an audio source.",
            font=("Segoe UI", 9, "normal"),
            color=palette["mutedForeground"],
            
            wraplength=850,
        ).pack(anchor="w", padx=6, pady=(7, 0))

        cw_card = make_card(page, padding=16)
        cw_card.pack(fill="x", pady=(0, 12))
        make_label(
            cw_card,
            "CW message",
            font=("Segoe UI", 15, "bold"),
            
        ).pack(anchor="w", pady=(0, 8))
        cw_fields = ttk.Frame(cw_card)
        cw_fields.pack(fill="x")
        cw_fields.grid_columnconfigure(0, weight=3, uniform="cw-fields")
        cw_fields.grid_columnconfigure(1, weight=1, uniform="cw-fields")
        add_tx_entry(cw_fields, "Message", tx_cw_text_var, column=0, row=0)
        add_tx_entry(cw_fields, "Speed (5-60 WPM)", tx_cw_wpm_var, column=1, row=0)
        cw_loop = ttk.Checkbutton(
            cw_card,
            text="Repeat CW message",
            variable=tx_cw_loop_var,
            cursor="hand2",
            takefocus=True,
        )
        cw_loop.pack(anchor="w", padx=6, pady=(8, 0))

        connect_card = make_card(page, padding=16)
        connect_card.pack(fill="x", pady=(0, 12))
        make_label(
            connect_card,
            "How to connect",
            font=("Segoe UI", 15, "bold"),
            
        ).pack(anchor="w")
        make_label(
            connect_card,
            "For TXITY, connect your SDR++ client to the virtual SDR++ Server endpoint shown by TXITY. "
            "Choose a microphone, desktop-audio device, or playlist in MultiV, then start the transmission. "
            "MultiV sends audio to the virtual service; it does not control local RF hardware.",
            font=("Segoe UI", 9, "normal"),
            color=palette["mutedForeground"],
            
            wraplength=850,
        ).pack(anchor="w", pady=(6, 0))

        drm_progressbar = ttk.Progressbar(
            signal_card,
            mode="indeterminate",
            maximum=100,
        )

        make_label(
            page,
            "One selected audio input can feed TXITY and every enabled Icecast destination at the same time.",
            font=("Segoe UI", 9, "normal"),
            color=palette["mutedForeground"],
            wraplength=850,
        ).pack(anchor="w", pady=(0, 8))

        # Put input selection and output routing first in the transmit workflow.
        audio_card.pack_configure(before=station_card)
        icecast_card.pack_configure(before=station_card)

    def show_station_detail(parent: ttk.Frame) -> None:
        row = next(
            (item for item in station_rows if str(item.get("id", "")) == selected_station_id),
            None,
        )
        if row is None:
            make_button(parent, "Open station directory", open_station_directory, kind="tonal").pack(anchor="w")
            make_label(
                parent,
                "This station is no longer in the current listing.",
                color=palette["mutedForeground"],
            ).pack(anchor="w", pady=18)
            return

        page, _canvas = create_scrollable_page(parent)
        make_button(
            page,
            "← Back to stations",
            open_station_directory,
            kind="text",
            padx=10,
            pady=7,
        ).pack(anchor="w", pady=(5, 12))

        make_label(
            page,
            "LIVE STATION",
            font=("Segoe UI", 9, "bold"),
            color=palette["accent"],
        ).pack(anchor="w")
        make_label(
            page,
            str(row.get("station", "Unknown station")),
            font=("Segoe UI", 28, "bold"),
            wraplength=900,
        ).pack(anchor="w", pady=(2, 1))
        short_description = str(
            row.get("description")
            or row.get("details_text")
            or "No short description has been added."
        )
        make_label(
            page,
            short_description,
            font=("Segoe UI", 11, "normal"),
            color=palette["mutedForeground"],
            wraplength=900,
        ).pack(anchor="w", pady=(0, 16))

        profile = make_card(page, padding=16)
        profile.pack(fill="x", pady=(0, 12))
        upper = ttk.Frame(profile)
        upper.pack(fill="x")

        logo = make_image_label(upper, str(row.get("logo_url") or ""), size=176, placeholder="ST", role=f"station profile logo: {row.get('station', 'unknown')}")
        logo.pack(side="left", padx=(0, 16), pady=(0, 12))

        heading = ttk.Frame(upper)
        heading.pack(side="left", fill="both", expand=True)
        make_label(
            heading,
            str(row.get("frequency") or "Frequency not reported"),
            font=("Segoe UI", 18, "bold"),
            color=palette["accent"],
            
        ).pack(anchor="w", pady=(4, 8))
        make_label(
            heading,
            f"{row.get('mode') or 'Mode not reported'}  ·  {row.get('on_air') or 'On-air time not reported'}",
            font=("Segoe UI", 10, "normal"),
            color=palette["mutedForeground"],
            
        ).pack(anchor="w", pady=(0, 8))
        make_label(
            heading,
            f"★ {row.get('rating_average'):.2f} ({row.get('rating_count', 0)})"
            if isinstance(row.get("rating_average"), (int, float)) and row.get("rating_count")
            else "Not rated yet",
            font=("Segoe UI", 11, "bold"),
            
        ).pack(anchor="w")
        make_label(
            heading,
            f"♡ {row.get('likes', 0)} likes",
            font=("Segoe UI", 10, "normal"),
            color=palette["mutedForeground"],
            
        ).pack(anchor="w", pady=(7, 0))

        divider = ttk.Separator(profile, orient="horizontal")
        divider.pack(fill="x", pady=(4, 14))

        owner = ttk.Frame(profile)
        owner.pack(fill="x")
        avatar_url = str(row.get("avatar_url") or "")
        avatar = make_image_label(
            owner,
            avatar_url,
            size=42,
            placeholder="D",
            role=f"station owner avatar: {row.get('station', 'unknown')}",
        )
        avatar.pack(side="left", padx=(0, 10))
        make_label(
            owner,
            f"Discord user: {row.get('operator') or 'Unknown'}",
            font=("Segoe UI", 10, "normal"),
            
        ).pack(side="left", anchor="center")

        profile_details = str(row.get("details_text") or "").strip()
        if profile_details:
            make_label(
                profile,
                profile_details,
                font=("Segoe UI", 10, "normal"),
                
                wraplength=820,
                justify="left",
            ).pack(anchor="w", pady=(16, 2))

        page_layout = row.get("page_layout")
        if isinstance(page_layout, dict) and page_layout.get("blocks"):
            layout_card = make_card(page, padding=12)
            layout_card.pack(fill="x", pady=(0, 15))
            make_label(
                layout_card,
                "Station page",
                font=("Segoe UI", 15, "bold"),
                
            ).pack(anchor="w", pady=(0, 10))
            layout_height = page_layout.get("height", 420)
            layout_height = max(320, min(1_200, int(layout_height)))
            layout = ttk.Frame(
                layout_card,
                height=layout_height,
            )
            layout.pack(fill="x")
            layout.pack_propagate(False)
            for block in page_layout["blocks"]:
                block_type = block.get("type", "text")
                x = max(0.0, min(100.0, float(block.get("x", 8))))
                y = max(0.0, min(100.0, float(block.get("y", 8))))
                width = max(5.0, min(100.0, float(block.get("width", 84))))
                height = max(8.0, min(900.0, float(block.get("height", 72))))
                if block_type == "separator":
                    ttk.Separator(layout, orient="horizontal").place(
                        relx=x / 100, rely=y / 100, relwidth=width / 100, height=2
                    )
                elif block_type == "image" and block.get("image_url"):
                    image_block = make_image_label(
                        layout,
                        str(block.get("image_url")),
                        size=min(360, max(80, int(width * 5))),
                        placeholder="Image",
                        role=f"station page image: {row.get('station', 'unknown')}",
                    )
                    image_block.place(
                        relx=x / 100,
                        rely=y / 100,
                        relwidth=width / 100,
                        height=min(300, height),
                    )
                else:
                    font_name = str(block.get("font") or "Segoe UI")
                    if font_name not in {"Lexend", "Segoe UI", "Georgia", "Courier New", "Impact", "system-ui"}:
                        font_name = "Segoe UI"
                    font_size = int(block.get("font_size", 18))
                    font_weight = int(block.get("font_weight", 400))
                    font_style = "bold" if font_weight >= 600 else "normal"
                    text_align = str(block.get("align") or "left")
                    text_widget = make_label(
                        layout,
                        str(block.get("text") or ""),
                        font=(font_name if font_name != "system-ui" else "Segoe UI", font_size, font_style),
                        
                        justify=text_align,
                        anchor="center" if text_align == "center" else ("e" if text_align == "right" else "w"),
                        wraplength=620,
                    )
                    text_widget.place(
                        relx=x / 100,
                        rely=y / 100,
                        relwidth=width / 100,
                        height=min(300, height),
                    )
        else:
            details_text = profile_details or "No extra station details yet."
            details_card = make_card(page, padding=16)
            details_card.pack(fill="x", pady=(0, 12))
            make_label(
                details_card,
                "Station details",
                font=("Segoe UI", 15, "bold"),
                
            ).pack(anchor="w", pady=(0, 8))
            make_label(
                details_card,
                details_text,
                font=("Segoe UI", 10, "normal"),
                color=palette["mutedForeground"],
                
                wraplength=840,
            ).pack(anchor="w")

        gallery_urls = [
            url for url in row.get("image_urls", [])
            if isinstance(url, str) and url and url != row.get("logo_url")
        ]
        if gallery_urls:
            gallery_card = make_card(page, padding=16)
            gallery_card.pack(fill="x", pady=(0, 12))
            make_label(
                gallery_card,
                "Images",
                font=("Segoe UI", 15, "bold"),
                
            ).pack(anchor="w", pady=(0, 10))
            gallery = ttk.Frame(gallery_card)
            gallery.pack(anchor="w")
            for url in gallery_urls[:6]:
                make_image_label(gallery, url, size=140, placeholder="Image", role=f"station gallery: {row.get('station', 'unknown')}").pack(
                    side="left",
                    padx=(0, 10),
                )

        comments = row.get("comments") if isinstance(row.get("comments"), list) else []
        comments_card = make_card(page, padding=16)
        comments_card.pack(fill="x", pady=(0, 16))
        make_label(
            comments_card,
            f"Comments ({row.get('comment_count', len(comments))})",
            font=("Segoe UI", 16, "bold"),
            
        ).pack(anchor="w", pady=(0, 12))
        if not comments:
            make_label(
                comments_card,
                "No comments have been posted.",
                color=palette["mutedForeground"],
                
            ).pack(anchor="w")
        else:
            for comment in comments[:30]:
                comment_row = ttk.Frame(comments_card)
                comment_row.pack(fill="x", pady=(0, 12))
                comment_avatar = make_image_label(
                    comment_row,
                    str(comment.get("avatar_url") or ""),
                    size=40,
                    placeholder="D",
                    role=f"comment avatar: {comment.get('author', 'unknown')}",
                )
                comment_avatar.pack(side="left", padx=(0, 10))
                comment_body = ttk.Frame(comment_row)
                comment_body.pack(side="left", fill="x", expand=True)
                comment_title = str(comment.get("author") or "TXITY user")
                timestamp = str(comment.get("timestamp") or "")
                make_label(
                    comment_body,
                    f"{comment_title}  {timestamp}".strip(),
                    font=("Segoe UI", 9, "bold"),
                    
                ).pack(anchor="w")
                make_label(
                    comment_body,
                    str(comment.get("text") or ""),
                    font=("Segoe UI", 9, "normal"),
                    color=palette["mutedForeground"],
                    
                    wraplength=790,
                ).pack(anchor="w", pady=(3, 0))

    def finish_update_check(
        info: Any = None,
        error: str | None = None,
        *,
        prompt_update: bool = False,
        report_errors: bool = False,
        report_current: bool = False,
    ) -> None:
        update_state["checking"] = False
        if update_check_button is not None:
            update_check_button.configure(state="normal")
        if error:
            update_status_var.set(f"Update check failed: {error}")
            if report_errors:
                messagebox.showerror("Update check failed", error, parent=root)
            return
        if info is None:
            update_state["info"] = None
            update_status_var.set(
                f"MultiV {APP_VERSION} is up to date."
                if is_packaged()
                else "Automatic updates are available in packaged builds."
            )
            if update_install_button is not None:
                update_install_button.configure(state="disabled")
            if report_current:
                messagebox.showinfo(
                    "No updates available",
                    "This copy of MultiV is up to date.",
                    parent=root,
                )
            return

        update_state["info"] = info
        update_status_var.set(
            f"Update available: {info.version}."
        )
        if update_install_button is not None:
            update_install_button.configure(state="normal")
        if prompt_update and messagebox.askyesno(
            "MultiV update available",
            f"Version {info.version} is available. Install it and restart MultiV? "
            "Any active transmission will be stopped.",
            parent=root,
        ):
            install_application_update(confirmed=True)

    def check_application_updates(
        *,
        prompt_update: bool = False,
        report_errors: bool = False,
        report_current: bool = False,
    ) -> None:
        if not is_packaged():
            update_status_var.set("Automatic updates are available in packaged builds.")
            return
        if update_state["checking"]:
            return
        update_state["checking"] = True
        update_status_var.set("Checking for updates...")
        if update_check_button is not None:
            update_check_button.configure(state="disabled")

        def worker() -> None:
            try:
                info = check_for_update()
            except Exception as exc:
                details = safe_error_text(str(exc), 500)
                root.after(
                    0,
                    lambda message=details: finish_update_check(
                        error=message,
                        report_errors=report_errors,
                        report_current=report_current,
                    ),
                )
            else:
                root.after(
                    0,
                    lambda result=info: finish_update_check(
                        result,
                        prompt_update=prompt_update,
                        report_errors=report_errors,
                        report_current=report_current,
                    ),
                )

        threading.Thread(
            target=worker,
            daemon=True,
            name="multiv-update-check",
        ).start()

    def install_application_update(*, confirmed: bool = False) -> None:
        info = update_state.get("info")
        if info is None:
            return
        if not confirmed and not messagebox.askyesno(
            "Install MultiV update",
            f"Download and install MultiV {info.version}? MultiV will close, stop any "
            "active transmission, replace its application files, and restart.",
            parent=root,
        ):
            return

        update_status_var.set("Downloading and verifying the update...")
        if update_install_button is not None:
            update_install_button.configure(state="disabled")
        if update_check_button is not None:
            update_check_button.configure(state="disabled")

        def worker() -> None:
            try:
                install_update(info)
            except Exception as exc:
                details = safe_error_text(str(exc), 700)
                root.after(0, lambda message=details: update_install_failed(message))
            else:
                root.after(0, on_shutdown)

        def update_install_failed(message: str) -> None:
            update_status_var.set(f"Update failed: {message}")
            if update_check_button is not None:
                update_check_button.configure(state="normal")
            if update_install_button is not None:
                update_install_button.configure(state="normal")
            messagebox.showerror("MultiV update failed", message, parent=root)

        threading.Thread(
            target=worker,
            daemon=True,
            name="multiv-update-install",
        ).start()

    def show_settings(parent: ttk.Frame) -> None:
        nonlocal station_logo_upload_button, update_check_button, update_install_button
        page, _canvas = create_scrollable_page(parent)
        make_label(
            page,
            "Settings",
            font=("Segoe UI", 25, "bold"),
        ).pack(anchor="w", pady=(8, 18))

        profile_card = make_card(page, padding=16)
        profile_card.pack(fill="x", pady=(0, 14))
        make_label(
            profile_card,
            "Station profile",
            font=("Segoe UI", 16, "bold"),
            
        ).pack(anchor="w", pady=(0, 6))
        make_label(
            profile_card,
            "MultiV sends these values to TXITY as your public station logo and station-page details.",
            font=("Segoe UI", 9, "normal"),
            color=palette["mutedForeground"],
            
            wraplength=820,
        ).pack(anchor="w", pady=(0, 14))

        logo_frame = ttk.Frame(profile_card)
        logo_frame.pack(fill="x", pady=(0, 12))
        make_label(
            logo_frame,
            "Station logo URL",
            font=("Segoe UI", 9, "bold"),
            color=palette["mutedForeground"],
            
        ).pack(anchor="w", pady=(0, 5))
        logo_entry = ttk.Entry(
            logo_frame,
            textvariable=tx_logo_url_var,
            font=("Segoe UI", 10),
        )
        logo_entry.pack(fill="x", ipady=7)

        def choose_station_logo() -> None:
            selected_path = filedialog.askopenfilename(
                parent=root,
                title="Choose station logo",
                filetypes=(
                    ("Image files", "*.png *.jpg *.jpeg *.webp *.gif"),
                    ("All files", "*.*"),
                ),
            )
            if not selected_path:
                return
            try:
                client.upload_station_image_file(selected_path, kind="logo")
            except Exception as exc:
                tx_logo_upload_status_var.set(safe_error_text(str(exc), 300))
                return

            tx_logo_upload_status_var.set(f"Uploading {Path(selected_path).name}...")
            set_station_logo_upload_button_enabled(False)

        station_logo_upload_button = make_button(
            logo_frame,
            "Upload image",
            choose_station_logo,
            kind="tonal",
            padx=14,
            pady=8,
        )
        station_logo_upload_button.pack(anchor="w", pady=(8, 0))
        if client.station_image_uploading:
            station_logo_upload_button.configure(state="disabled")

        make_label(
            logo_frame,
            "PNG, JPEG, WebP, or GIF. Maximum file size: 5 MB.",
            font=("Segoe UI", 8, "normal"),
            color=palette["mutedForeground"],
            
        ).pack(anchor="w", pady=(5, 0))

        make_label(
            logo_frame,
            variable=tx_logo_upload_status_var,
            font=("Segoe UI", 9, "normal"),
            color=palette["mutedForeground"],
            
            wraplength=820,
        ).pack(anchor="w", pady=(5, 0))

        make_label(
            profile_card,
            "Station page details",
            font=("Segoe UI", 9, "bold"),
            color=palette["mutedForeground"],
            
        ).pack(anchor="w", pady=(0, 5))
        details_text = tk.Text(
            profile_card,
            height=6,
            wrap="word",
            font=("Segoe UI", 10),
            bg=palette["background"],
            fg=palette["foreground"],
            insertbackground=palette["foreground"],
            relief="flat",
            borderwidth=1,
            highlightthickness=1,
            highlightbackground=palette["outline"],
            highlightcolor=palette["accent"],
        )
        details_text.insert("1.0", tx_details_var.get())
        details_text.pack(fill="x", ipady=5)

        def save_details_text(_event=None) -> None:
            tx_details_var.set(details_text.get("1.0", "end-1c")[:1200])

        details_text.bind("<KeyRelease>", save_details_text)
        details_text.bind("<FocusOut>", save_details_text)
        make_label(
            profile_card,
            "Displayed when someone opens your station page. Maximum 1,200 characters.",
            font=("Segoe UI", 9, "normal"),
            color=palette["mutedForeground"],
            
        ).pack(anchor="w", pady=(6, 0))

        web_card = make_card(page, padding=16)
        web_card.pack(fill="x")
        make_label(
            web_card,
            "Browser control panel",
            font=("Segoe UI", 16, "bold"),
            
        ).pack(anchor="w", pady=(0, 6))
        make_label(
            web_card,
            "Serve the MultiV control interface to devices on your local network, using the TXITY transmit and station-page layout.",
            font=("Segoe UI", 9, "normal"),
            color=palette["mutedForeground"],
            
            wraplength=820,
        ).pack(anchor="w", pady=(0, 10))

        enabled = ttk.Checkbutton(
            web_card,
            text="Enable Web GUI",
            variable=web_gui_enabled_var,
            command=toggle_web_gui,
        )
        enabled.pack(anchor="w", pady=(2, 12))

        make_label(
            web_card,
            "Set or change password",
            font=("Segoe UI", 9, "bold"),
            color=palette["mutedForeground"],
            
        ).pack(anchor="w", pady=(0, 5))
        password_row = ttk.Frame(web_card)
        password_row.pack(fill="x")
        password_entry = ttk.Entry(
            password_row,
            textvariable=web_gui_password_var,
            show="*",
            font=("Segoe UI", 10),
        )
        password_entry.pack(side="left", fill="x", expand=True, ipady=7, padx=(0, 8))
        make_label(
            web_card,
            "Use at least 12 characters. MultiV stores a salted PBKDF2 hash of your password.",
            font=("Segoe UI", 9, "normal"),
            color=palette["mutedForeground"],
            
        ).pack(anchor="w", pady=(5, 8))
        make_button(
            password_row,
            "Save password",
            save_web_gui_password,
            kind="primary",
        ).pack(side="left")

        make_label(
            web_card,
            variable=web_gui_status_var,
            font=("Segoe UI", 9, "normal"),
            color=palette["accent"],
            
            wraplength=820,
        ).pack(anchor="w", pady=(14, 6))
        make_label(
            web_card,
            "Address: http://127.0.0.1:23234 on this computer. Other devices use this computer's LAN IP on port 23234. The password is hashed in the settings file, but HTTP does not encrypt traffic. Use this only on a trusted network and do not forward the port to the internet.",
            font=("Segoe UI", 9, "normal"),
            color=palette["mutedForeground"],
            
            wraplength=820,
        ).pack(anchor="w", pady=(4, 10))
        make_button(
            web_card,
            "Open Web GUI",
            lambda: webbrowser.open("http://127.0.0.1:23234"),
            kind="tonal",
        ).pack(anchor="w")

        update_card = make_card(page, padding=16)
        update_card.pack(fill="x", pady=(0, 14))
        make_label(
            update_card,
            "Application updates",
            font=("Segoe UI", 16, "bold"),
            
        ).pack(anchor="w", pady=(0, 6))
        make_label(
            update_card,
            f"Version {APP_VERSION}",
            font=("Segoe UI", 9, "normal"),
            color=palette["mutedForeground"],
            
        ).pack(anchor="w", pady=(0, 6))
        make_label(
            update_card,
            variable=update_status_var,
            font=("Segoe UI", 9, "normal"),
            color=palette["mutedForeground"],
            
            wraplength=820,
        ).pack(anchor="w", pady=(0, 10))
        update_buttons = ttk.Frame(update_card)
        update_buttons.pack(anchor="w")
        update_check_button = make_button(
            update_buttons,
            "Check for updates",
            lambda: check_application_updates(
                report_errors=True,
                report_current=True,
            ),
            kind="tonal",
        )
        update_check_button.pack(side="left", padx=(0, 8))
        update_install_button = make_button(
            update_buttons,
            "Install update",
            install_application_update,
            kind="primary",
        )
        update_install_button.pack(side="left")
        if not is_packaged():
            update_check_button.configure(state="disabled")
            update_install_button.configure(state="disabled")
        else:
            update_install_button.configure(state="disabled")

    def show_discord(parent: ttk.Frame) -> None:
        page = ttk.Frame(parent)
        page.pack(fill="both", expand=True)

        make_label(
            page,
            "Discord account",
            font=("Segoe UI", 25, "bold"),
        ).pack(anchor="w", pady=(8, 18))

        card = make_card(page, padding=16)
        card.pack(fill="x")

        make_label(
            card,
            variable=account_var,
            font=("Segoe UI", 15, "bold"),
            
        ).pack(anchor="w")

        make_label(
            card,
            variable=auth_status_var,
            font=("Segoe UI", 10, "normal"),
            color=palette["mutedForeground"],
            
            wraplength=780,
        ).pack(anchor="w", pady=(10, 18))

        buttons = ttk.Frame(card)
        buttons.pack(anchor="w")
        make_button(
            buttons,
            "Sign in with Discord",
            on_sign_in,
            kind="primary",
        ).pack(side="left", padx=(0, 8))
        make_button(
            buttons,
            "Disconnect Discord",
            disconnect_discord,
            kind="tonal",
        ).pack(side="left")

        description = (
            "MultiV uses TXITY's official Discord sign-in flow. Your website session "
            "uses a separate browser profile. MultiV checks it each time the "
            "desktop app starts. MultiV saves the TXITY session token in its per-user "
            "configuration folder so headless mode can use it."
        )
        make_label(
            card,
            description,
            font=("Segoe UI", 9, "normal"),
            color=palette["mutedForeground"],
            
            wraplength=780,
        ).pack(anchor="w", pady=(22, 0))

        make_label(
            page,
            f"Server: {BASE_URL}",
            font=("Segoe UI", 9, "normal"),
            color=palette["accent"],
        ).pack(anchor="w", pady=(18, 0))

    def build_shell() -> None:
        nonlocal shell_frame, content_frame, nav_frame, station_cards_frame, tx_transmit_button

        nonlocal active_scroll_canvas
        active_scroll_canvas = None
        for child in root.winfo_children():
            child.destroy()

        station_cards_frame = None
        nav_frame = None
        tx_transmit_button = None
        shell_frame = ttk.Frame(root)
        shell_frame.pack(fill="both", expand=True)

        topbar = ttk.Frame(shell_frame, padding=(28, 8))
        topbar.configure(height=64)
        topbar.pack(fill="x", padx=18, pady=(12, 10))
        topbar.pack_propagate(False)

        make_label(
            topbar,
            "MultiV",
            font=("Segoe UI", 17, "bold"),
            color=palette["accent"],
            
        ).pack(side="left", padx=(2, 12))

        if discord_authenticated:
            navigation = ttk.Frame(topbar)
            navigation.pack(side="left", fill="y")

            destinations = (
                ("Transmit", "transmit"),
                ("Discord", "discord"),
                ("Settings", "settings"),
            )
            for label, destination in destinations:
                selected = current_page == destination
                nav_button = make_button(
                    navigation,
                    label,
                    lambda target=destination: navigate(target),
                    kind="primary" if selected else "normal",
                    padx=12,
                    pady=7,
                )
                nav_button.pack(side="left", padx=2, pady=2)

            txity_menu = ttk.Menubutton(topbar, text="TXITY ▾")
            txity_menu.pack(side="left", padx=(8, 0))
            txity_menu_items = tk.Menu(
                txity_menu,
                tearoff=False,
                background=palette["background"],
                foreground=palette["foreground"],
                activebackground=palette["selectionBackground"],
                activeforeground=palette["selectionForeground"],
            )
            txity_menu_items.add_command(
                label="Open TXITY website",
                command=lambda: webbrowser.open(BASE_URL),
            )
            txity_menu_items.add_command(
                label="Station directory",
                command=open_station_directory,
            )
            txity_menu.configure(menu=txity_menu_items)

            make_button(
                topbar,
                "Light mode" if theme_mode == "dark" else "Dark mode",
                toggle_theme,
                kind="tonal",
                padx=12,
                pady=7,
            ).pack(side="right", padx=(8, 2))

            make_label(
                topbar,
                variable=connection_var,
                font=("Segoe UI", 8, "normal"),
                color=palette["mutedForeground"],
                
            ).pack(side="right", padx=(6, 8))

            content_frame = ttk.Frame(shell_frame, padding=(20, 10))
            content_frame.pack(fill="both", expand=True)

            if current_page == "transmit":
                show_transmit(content_frame)
            elif current_page == "settings":
                show_settings(content_frame)
            elif current_page == "station_detail":
                show_station_detail(content_frame)
            elif current_page == "discord":
                show_discord(content_frame)
            else:
                show_transmit(content_frame)
        else:
            make_button(
                topbar,
                "Settings",
                lambda: navigate("settings"),
                kind="text",
                padx=10,
                pady=8,
            ).pack(side="left", padx=(8, 0))
            make_button(
                topbar,
                "Light mode" if theme_mode == "dark" else "Dark mode",
                toggle_theme,
                kind="tonal",
                padx=12,
                pady=7,
            ).pack(side="right")
            content_frame = ttk.Frame(shell_frame, padding=(20, 16))
            content_frame.pack(fill="both", expand=True)
            if current_page == "settings":
                show_settings(content_frame)
            else:
                show_gate(content_frame)

    def apply_status(data: dict[str, Any]) -> None:
        nonlocal server_band_min_hz, server_band_max_hz
        nonlocal tx_negative_status_reason, tx_negative_status_count
        nonlocal tx_ptt_confirmed, tx_terminal_server_message, frequency_retry_context
        nonlocal tx_last_status_signature
        low, high = data.get("bandMinHz"), data.get("bandMaxHz")

        if transmitter.is_busy:
            status_signature = tuple(
                data.get(key)
                for key in (
                    "ptt",
                    "txStartUsable",
                    "txStartReason",
                    "txBlockReason",
                    "queuePosition",
                )
            )
            if status_signature != tx_last_status_signature:
                tx_last_status_signature = status_signature
                status_fields = {
                    key: data.get(key)
                    for key in (
                        "ptt",
                        "txStartUsable",
                        "txStartReason",
                        "txBlockReason",
                        "queuePosition",
                    )
                    if key in data
                }
                client.events.put({
                    "kind": "tx_log",
                    "text": "TXITY status changed: " + json.dumps(status_fields, sort_keys=True),
                })
        else:
            tx_last_status_signature = None
        if isinstance(low, (int, float)) and isinstance(high, (int, float)):
            server_band_min_hz = int(low)
            server_band_max_hz = int(high)
        else:
            server_band_min_hz = None
            server_band_max_hz = None
        update_frequency_check()

        if transmitter.is_busy and data.get("ptt") is True:
            frequency_retry_context = None
            if not tx_ptt_confirmed:
                tx_ptt_confirmed = True
            tx_negative_status_reason = None
            tx_negative_status_count = 0
            tx_status_var.set("TXITY confirms that the station is transmitting.")
        elif transmitter.is_busy and data.get("ptt") is False:
            if frequency_retry_context is not None and frequency_retry_context.get("pending"):
                candidate = frequency_retry_context.get("queued_candidate")
                if isinstance(candidate, tuple) and len(candidate) == 2:
                    tx_status_var.set(
                        f"Frequency denied. Switching to {candidate[0]}..."
                    )
                return
            queue_position = data.get("queuePosition")
            if isinstance(queue_position, int) and queue_position >= 0:
                tx_negative_status_reason = None
                tx_negative_status_count = 0
                tx_status_var.set(f"Waiting in the TXITY transmitter queue (position {queue_position + 1}).")
            elif data.get("txStartUsable") is False or data.get("txBlockReason") not in (None, False, ""):
                reason = data.get("txStartReason", data.get("txBlockReason"))
                reasons = {
                    2: "TXITY's transmitter capacity is full",
                    3: "frequency sharing must be enabled by both stations",
                    4: "the total transmitter limit has been reached",
                    5: "enter a station name",
                    "2": "TXITY's transmitter capacity is full",
                    "3": "frequency sharing must be enabled by both stations",
                    "4": "the total transmitter limit has been reached",
                    "5": "enter a station name",
                }
                reason_text = reasons.get(reason, "TXITY did not accept the transmission request")
                reason_key = str(reason if reason is not None else "txStartUsable=false")
                if not transmitter.server_rejection_ready:
                    tx_status_var.set("Waiting for TXITY to confirm the transmission request...")
                else:
                    if reason_key == tx_negative_status_reason:
                        tx_negative_status_count += 1
                    else:
                        tx_negative_status_reason = reason_key
                        tx_negative_status_count = 1
                    if tx_negative_status_count >= 2:
                        if tx_ptt_confirmed:
                            message = (
                                "TXITY reported that PTT stopped after confirming the transmission: "
                                f"{reason_text} (reason code {reason_key})."
                            )
                        else:
                            message = (
                                "TXITY did not confirm the PTT request: "
                                f"{reason_text} (reason code {reason_key})."
                            )
                        context = frequency_retry_context
                        can_retry_frequency = (
                            reason_key == "3"
                            and not tx_ptt_confirmed
                            and isinstance(context, dict)
                            and not context.get("pending")
                            and int(context.get("next_index", 0)) < len(context.get("candidates", []))
                        )
                        if can_retry_frequency:
                            next_index = int(context["next_index"])
                            candidate = context["candidates"][next_index]
                            context["next_index"] = next_index + 1
                            context["queued_candidate"] = candidate
                            context["pending"] = True
                            current_index = int(context.get("current_index", 0))
                            current_text = context["candidates"][current_index][0]
                            tx_terminal_server_message = None
                            tx_status_var.set(
                                f"TXITY denied {current_text}. Trying alternate "
                                f"frequency {next_index + 1} of {len(context['candidates'])}: "
                                f"{candidate[0]}..."
                            )
                            transmitter.stop()
                        else:
                            if reason_key == "3" and isinstance(context, dict) and len(context.get("candidates", [])) > 1:
                                message = (
                                    f"TXITY rejected all {len(context['candidates'])} configured "
                                    f"frequencies. {reason_text} (reason code {reason_key})."
                                )
                            tx_terminal_server_message = message
                            frequency_retry_context = None
                            cancel_pending_live_reconfigure()
                            tx_status_var.set(message)
                            transmitter.stop()
            else:
                tx_negative_status_reason = None
                tx_negative_status_count = 0

    def watch_playlist_sources() -> None:
        """Refresh imported folders and files when their contents change on disk."""
        nonlocal playlist_watch_signature
        while not playlist_watch_stop.wait(4.0):
            with playlist_watch_lock:
                sources = list(tx_playlist_sources)
                excluded = set(tx_playlist_excluded_files)
                revision = playlist_watch_revision
            if not sources:
                continue

            try:
                files, signature = build_playlist_snapshot(sources, excluded)
            except Exception as exc:
                details = safe_error_text(str(exc), 300)
                continue

            if playlist_watch_stop.is_set():
                break

            with playlist_watch_lock:
                if revision != playlist_watch_revision:
                    continue
                if signature == playlist_watch_signature:
                    continue
                playlist_watch_signature = signature

            client.events.put({
                "kind": "playlist_refresh",
                "files": files,
                "revision": revision,
            })

    def drain_events() -> None:
        nonlocal discord_authenticated, auth_restore_started, auth_enabled, station_rows, current_page, tx_profile_initialized
        nonlocal users_listing_received, frequency_retry_context
        nonlocal tx_negative_status_reason, tx_negative_status_count
        nonlocal tx_ptt_confirmed, tx_terminal_server_message, tx_last_status_signature
        while True:
            try:
                event = client.events.get_nowait()
            except queue.Empty:
                break

            kind = event.get("kind")
            if kind == "connection":
                text = str(event.get("text", ""))
                connection_var.set(text)

            elif kind == "tx_log":
                log_text = str(event.get("text") or "").strip()
                if log_text:
                    print(
                        "[MultiV] " + log_text[:1_500],
                        file=sys.stderr,
                        flush=True,
                    )

            elif kind == "status":
                apply_status(event.get("data", {}))

            elif kind == "tx_state":
                state = str(event.get("state") or "")
                state_text = str(event.get("text") or state)
                print(
                    f"[MultiV] Transmitter state: {state}. {state_text}",
                    file=sys.stderr,
                    flush=True,
                )
                live_restart = state == "stopped" and live_reconfigure_state["stopping"]
                retry_after_stop = (
                    state == "stopped"
                    and not live_restart
                    and isinstance(frequency_retry_context, dict)
                    and bool(frequency_retry_context.get("pending"))
                )
                if state == "starting":
                    tx_last_status_signature = None
                    tx_negative_status_reason = None
                    tx_negative_status_count = 0
                    tx_ptt_confirmed = False
                    tx_terminal_server_message = None
                elif state == "stopped":
                    tx_negative_status_reason = None
                    tx_negative_status_count = 0
                    tx_ptt_confirmed = False

                if live_restart:
                    was_cancelled = bool(live_reconfigure_state["cancelled"])
                    previous_status = tx_status_var.get()
                    start_pending_live_reconfigure()
                    if (
                        not transmitter.is_busy
                        and was_cancelled
                        and previous_status.startswith("Stopping transmission")
                    ):
                        tx_status_var.set("Transmission stopped.")
                elif state == "stopped":
                    live_reconfigure_state["active_request"] = None
                    if tx_terminal_server_message:
                        tx_status_var.set(tx_terminal_server_message)
                    else:
                        tx_status_var.set(state_text)
                else:
                    tx_status_var.set(state_text)

                if tx_audio_source_var.get() == "Audio playlist":
                    if state == "transmitting":
                        tx_playlist_status_var.set("Connected. Loading the first track...")
                    elif state == "stopped" and not transmitter.is_busy and (
                        tx_playlist_status_var.get().startswith("Preparing")
                        or tx_playlist_status_var.get().startswith("Playing")
                        or tx_playlist_status_var.get().startswith("Connected")
                    ):
                        tx_playlist_status_var.set("Playlist stopped before completion.")
                if retry_after_stop:
                    start_pending_frequency_retry()
                update_transmit_button()

            elif kind == "tx_error":
                cancel_pending_live_reconfigure()
                details = safe_error_text(str(event.get("text") or "Transmission failed."), 800)
                traceback_text = str(event.get("traceback") or "").strip()
                tx_status_var.set(f"Transmission error: {details}")
                if traceback_text:
                    print(
                        "\n[MultiV transmission error]\n"
                        + traceback_text,
                        file=sys.stderr,
                        flush=True,
                    )
                else:
                    print(
                        f"\n[MultiV transmission error] {details}\n",
                        file=sys.stderr,
                        flush=True,
                    )
                update_transmit_button()

            elif kind == "tx_icecast_state":
                destination_name = str(event.get("name") or "Icecast")
                destination_state = str(event.get("state") or "Unknown")
                destination_text = safe_error_text(
                    str(event.get("text") or "Icecast output status changed."),
                    400,
                )
                if destination_name != "Icecast":
                    tx_icecast_states[destination_name] = destination_state
                    refresh_icecast_list()
                tx_icecast_status_var.set(destination_text)

            elif kind == "tx_audio_warning":
                details = safe_error_text(str(event.get("text") or ""), 300)
                if details:
                    tx_status_var.set(f"Audio input warning: {details}")

            elif kind == "tx_server_message":
                data = event.get("data", {})
                if isinstance(data, dict):
                    message_type = str(data.get("type") or "")
                    server_text = safe_error_text(
                        str(data.get("message") or data.get("reason") or message_type),
                        500,
                    )
                    tx_status_var.set(f"TXITY: {server_text}")
                    if message_type in {"kicked", "banned"}:
                        frequency_retry_context = None
                        cancel_pending_live_reconfigure()
                        transmitter.stop()

            elif kind == "playlist_refresh":
                revision = event.get("revision")
                with playlist_watch_lock:
                    current_revision = playlist_watch_revision
                if revision != current_revision:
                    continue

                refreshed_files = event.get("files")
                if not isinstance(refreshed_files, list):
                    continue
                previous_files = {path.casefold() for path in tx_playlist_files}
                tx_playlist_files[:] = [
                    str(path) for path in refreshed_files if isinstance(path, str)
                ]
                current_files = {path.casefold() for path in tx_playlist_files}
                added = len(current_files - previous_files)
                removed = len(previous_files - current_files)

                if playlist_ui_refresh_callback is not None:
                    try:
                        playlist_ui_refresh_callback()
                    except tk.TclError:
                        pass
                schedule_tx_settings_save()

                if transmitter.is_busy and tx_audio_source_var.get() == "Audio playlist":
                    tx_playlist_status_var.set(
                        f"Playlist changed on disk ({added} added, {removed} removed). "
                        "Applying the updated playlist..."
                    )
                    schedule_live_reconfigure()
                elif transmitter.is_busy:
                    tx_playlist_status_var.set(
                        f"Playlist changed on disk ({added} added, {removed} removed)."
                    )
                elif added or removed:
                    tx_playlist_status_var.set(
                        f"Playlist refreshed from disk: {added} added, {removed} removed."
                    )
                else:
                    tx_playlist_status_var.set("Playlist files changed on disk. List refreshed.")

            elif kind == "tx_playlist_track":
                track_path = str(event.get("path") or "")
                track_name = Path(track_path).name or "Audio track"
                tx_playlist_status_var.set(
                    f"Playing {event.get('index', 0)} of {event.get('count', 0)}: {track_name}"
                )

            elif kind == "tx_playlist_finished":
                tx_playlist_status_var.set("Playlist finished.")

            elif kind == "tx_playlist_error":
                details = safe_error_text(str(event.get("text") or "Unknown audio decoder error."), 500)
                tx_playlist_status_var.set(f"Playlist error: {details}")

            elif kind == "station_image_upload_success":
                uploaded_url = str(event.get("url") or "").strip()
                if uploaded_url:
                    tx_logo_url_var.set(uploaded_url[:512])
                    tx_logo_upload_status_var.set(
                        "Logo uploaded. The hosted URL is in the Station logo URL field."
                    )
                else:
                    tx_logo_upload_status_var.set(
                        "TXITY did not return a usable image URL."
                    )
                set_station_logo_upload_button_enabled(True)

            elif kind == "station_image_upload_error":
                details = safe_error_text(
                    str(event.get("text") or "Station logo upload failed."),
                    500,
                )
                tx_logo_upload_status_var.set(details)
                set_station_logo_upload_button_enabled(True)

            elif kind == "config":
                config = event.get("data", {})
                if isinstance(config, dict):
                    auth_manager.configure(
                        config.get("discordAuthUrl"),
                        config.get("secureDiscordAuth") is True,
                    )
                    auth_enabled = config.get("secureDiscordAuth") is True
                    if auth_enabled and not auth_restore_started:
                        auth_restore_started = True
                        if os.environ.get("MULTIV_DISABLE_BROWSER_RESTORE") == "1":
                            auth_status_var.set(
                                "Automatic browser session restore is disabled."
                            )
                        else:
                            account_var.set("Checking saved Discord login...")
                            auth_status_var.set("Checking the saved TXITY browser session.")
                            auth_manager.restore_session()
                    elif not auth_enabled:
                        auth_status_var.set(
                            "TXITY secure Discord sign-in is not enabled. The dashboard remains locked."
                        )

            elif kind == "users_listing":
                users_listing_received = True
                station_rows = event.get("rows", [])
                update_frequency_check()
                if not tx_profile_initialized and client.discord_user:
                    own_id = str(client.discord_user.get("id") or "")
                    own_station = next(
                        (row for row in station_rows if str(row.get("discord_id") or "") == own_id),
                        None,
                    )
                    if own_station:
                        tx_station_name_var.set(str(own_station.get("station") or "")[:48])
                        tx_description_var.set(str(own_station.get("description") or "")[:180])
                        tx_logo_url_var.set(str(own_station.get("logo_url") or "")[:512])
                        tx_details_var.set(str(own_station.get("details_text") or "")[:1_200])
                        tx_page_layout_var.set(str(own_station.get("page_layout_json") or "")[:40_000])
                    elif not tx_station_name_var.get().strip():
                        display = (
                            client.discord_user.get("globalName")
                            or client.discord_user.get("username")
                            or ""
                        )
                        tx_station_name_var.set(str(display)[:48])
                    tx_profile_initialized = True
                image_cache.clear()
                image_loading.clear()
                image_labels.clear()
                station_status_var.set(
                    f"Loaded {len(station_rows)} stations from TXITY users.json."
                    if station_rows else
                    "TXITY users.json loaded, but its stations array is empty."
                )
                for station in station_rows:
                    station_name = safe_error_text(str(station.get("station") or "Unknown station"), 100)
                    description = str(station.get("description") or "").strip()
                    source = str(station.get("description_source") or "none")
                    if description:
                        pass
                    else:
                        pass
                    profile_details = str(station.get("details_text") or "").strip()
                    if profile_details:
                        pass
                    else:
                        pass
                update_station_tree()

            elif kind == "station_image":
                url = event.get("url")
                data = event.get("data")
                if isinstance(url, str) and isinstance(data, bytes):
                    apply_station_image(url, data)

            elif kind == "station_image_error":
                url = event.get("url")
                if isinstance(url, str):
                    image_loading.discard(url)
                    waiting = image_labels.pop(url, [])
                    roles = sorted({role for _label, _size, role in waiting})
                    role_text = ", ".join(roles) if roles else "no active image widgets"

            elif kind == "stations_error":
                details = safe_error_text(
                    str(event.get("text", "Station refresh failed.")),
                    max_chars=1_200,
                )
                station_status_var.set(details)
                update_station_tree()

            elif kind == "auth_token":
                tx_profile_initialized = False
                token = event.get("token")
                user = clean_discord_user(event.get("user"))
                if is_auth_token(token):
                    client.set_discord_auth(token, user)
                    discord_authenticated = True
                    display_name = (
                        (user or {}).get("globalName")
                        or (user or {}).get("username")
                        or "Discord account"
                    )
                    account_var.set(f"Signed in as {display_name}")
                    auth_status_var.set("Discord sign-in completed.")
                    station_status_var.set("Loading station data from TXITY...")
                    current_page = "transmit"
                    build_shell()
                    client.connect()
                else:
                    auth_status_var.set("TXITY returned an invalid Discord session.")

            elif kind == "auth_restore_missing":
                account_var.set("Discord sign-in required")
                auth_status_var.set("No saved Discord session was found. Sign in to continue.")
                build_shell()

            elif kind == "auth_signed_out":
                transmitter.stop(wait=0.5)
                tx_profile_initialized = False
                clear_auth_token()
                client.set_discord_auth(None, None)
                discord_authenticated = False
                account_var.set("Not signed in to Discord")
                auth_status_var.set("TXITY's Discord session was disconnected.")
                build_shell()

            elif kind == "auth_browser":
                auth_status_var.set(str(event.get("text", "")))

            elif kind == "auth_error":
                details = safe_error_text(
                    str(event.get("text", "Sign-in browser error")),
                    max_chars=16_000,
                )
                auth_status_var.set(details)

            elif kind == "socket_error":
                details = safe_error_text(
                    str(event.get("text", "Unknown status connection error")),
                    max_chars=1_500,
                )
                connection_var.set("Live status unavailable")

            elif kind == "error":
                details = safe_error_text(
                    str(event.get("text", "Network error")),
                    max_chars=1_500,
                )
                if not auth_enabled:
                    auth_status_var.set(details)

        root.after(250, drain_events)

    def periodic_station_refresh() -> None:
        if discord_authenticated:
            client.refresh_stations()
        root.after(60_000, periodic_station_refresh)

    if os.name == "nt":
        try:
            from system_tray import create_tray_icon

            tray_icon = create_tray_icon(
                on_restore=restore_from_tray,
                on_quit=quit_from_tray,
            )
            tray_icon.run_detached(setup=initialize_tray_icon)
        except Exception as exc:
            tray_icon = None

    root.bind("<Unmap>", handle_window_unmap, add="+")

    build_shell()
    root.protocol("WM_DELETE_WINDOW", on_shutdown)
    if web_gui_enabled_var.get():
        try:
            start_web_gui_server()
        except (OSError, ValueError) as exc:
            web_gui_enabled_var.set(False)
            save_tx_settings_now()
            web_gui_status_var.set(f"Could not start Web GUI: {exc}")
    root.after(150, poll_web_gui_commands)
    root.after(250, drain_events)
    root.after(60_000, periodic_station_refresh)
    if is_packaged():
        root.after(4_000, lambda: check_application_updates(prompt_update=True))
    playlist_watch_thread = threading.Thread(
        target=watch_playlist_sources,
        daemon=True,
        name="txitpy-playlist-watch",
    )
    playlist_watch_thread.start()
    client.load_public_data()
    root.mainloop()


if __name__ == "__main__":
    from cli import build_argument_parser, exit_headless_process, run_headless

    arguments = build_argument_parser().parse_args()
    if arguments.no_gui:
        exit_code = run_headless(
            arguments,
            client_factory=TxityClient,
            transmitter_factory=TxityTransmitter,
            default_settings_path=tx_settings_file(),
        )
        exit_headless_process(exit_code)

    if arguments.list_inputs or arguments.list_loopback_devices:
        raise SystemExit(
            run_headless(
                arguments,
                client_factory=TxityClient,
                transmitter_factory=TxityTransmitter,
                default_settings_path=tx_settings_file(),
            )
        )

    try:
        build_app()
    except Exception as exc:
        details = format_exception_details(exc)
        log_path = write_diagnostic(
            "main-window-startup",
            exc,
            filename="application-errors.log",
        )
        message = (
            "\n[MultiV main window startup error]\n"
            f"Exception: {type(exc).__name__}: {safe_error_text(exc)}\n\n"
            f"Traceback:\n{details}"
        )
        if log_path is not None:
            message += f"\n\nFull diagnostic log: {log_path}"
        print(message, file=sys.stderr, flush=True)
