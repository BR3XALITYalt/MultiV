"""Headless command-line runner for TXITPY."""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import signal
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable

from audio_playlist import build_playlist_snapshot, normalize_audio_sources
from discord_auth import auth_token_path, is_auth_token, safe_error_text
from transmitter import (
    SUPPORTED_MODES,
    build_frequency_candidates,
    build_transmit_config,
    list_audio_inputs,
    list_loopback_devices,
)


AUDIO_SOURCE_CHOICES = ("mic", "loopback", "playlist")


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="txitpy",
        description=(
            "TXITPY desktop client. Use --no-gui to load saved settings and "
            "start a transmission directly, for example under NSSM."
        ),
    )
    parser.add_argument(
        "--no-gui",
        "--nogui",
        action="store_true",
        help="Run headlessly and start transmitting with saved settings (alias: --nogui).",
    )
    parser.add_argument(
        "--config",
        type=Path,
        metavar="PATH",
        help="Use this transmit-settings.json instead of the default user settings file.",
    )
    parser.add_argument(
        "--auth-token-file",
        type=Path,
        metavar="PATH",
        help="Read the TXITY session token from a UTF-8 text file (default: the saved AppData token).",
    )
    parser.add_argument(
        "--auth-token-env",
        default="TXITPY_DISCORD_AUTH_TOKEN",
        metavar="NAME",
        help="Environment variable containing the session token (default: TXITPY_DISCORD_AUTH_TOKEN).",
    )
    parser.add_argument("--station-name", help="Override the saved station name.")
    parser.add_argument("--description", help="Override the saved station description.")
    parser.add_argument("--frequency", help="Override the saved frequency, for example 7.250 MHz.")
    parser.add_argument(
        "--mode",
        type=str.upper,
        choices=SUPPORTED_MODES,
        help="Override the saved mode.",
    )
    parser.add_argument("--power", type=float, help="Override station power in dBm.")
    parser.add_argument(
        "--audio-source",
        choices=AUDIO_SOURCE_CHOICES,
        help="Override the saved audio source.",
    )
    parser.add_argument(
        "--audio-device",
        metavar="NAME_OR_ID",
        help="Select an input device by its numeric ID or a unique part of its name.",
    )
    parser.add_argument(
        "--playlist",
        action="append",
        type=Path,
        metavar="PATH",
        help="Add a playlist file or folder. Repeat the option for multiple sources.",
    )
    parser.add_argument(
        "--shuffle",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Override playlist shuffle (use --no-shuffle to disable).",
    )
    parser.add_argument(
        "--loop",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Override playlist looping (use --no-loop to disable).",
    )
    parser.add_argument(
        "--allow-overlap",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Override frequency-sharing permission.",
    )
    parser.add_argument(
        "--connect-timeout",
        type=float,
        default=60.0,
        metavar="SECONDS",
        help="Maximum time to wait for the authenticated TXITY connection (default: 60).",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        metavar="PATH",
        help="Write a rotating service log to this file.",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
        type=str.upper,
        help="Console and file log level (default: INFO).",
    )
    parser.add_argument(
        "--list-inputs",
        action="store_true",
        help="List microphone and line-input devices, then exit.",
    )
    parser.add_argument(
        "--list-loopback-devices",
        action="store_true",
        help="List Windows desktop-audio loopback devices, then exit.",
    )
    return parser


def _configure_logging(args: argparse.Namespace) -> logging.Logger:
    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)
        try:
            handler.close()
        except OSError:
            pass

    root_logger.setLevel(getattr(logging, args.log_level))
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root_logger.addHandler(console)

    if args.log_file is not None:
        log_path = args.log_file.expanduser()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=4 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    return logging.getLogger("TXITPY")


def load_settings(path: Path) -> dict[str, Any]:
    """Load the same settings file used by the desktop client."""
    try:
        data = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read settings file {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"Settings file {path} must contain a JSON object.")
    return data


def _setting_text(settings: dict[str, Any], key: str, default: str) -> str:
    value = settings.get(key, default)
    return str(value) if value is not None else default


def _setting_bool(settings: dict[str, Any], key: str, default: bool) -> bool:
    value = settings.get(key, default)
    return value if isinstance(value, bool) else default


def _resolve_device(
    devices: list[tuple[int, str]],
    requested: str | None,
    *,
    device_type: str,
    allow_default: bool,
) -> tuple[int | None, str]:
    """Resolve a device ID or unique name fragment against current devices."""
    value = str(requested or "").strip()
    if not value or value.casefold() in {"default", "system default"}:
        if allow_default:
            return None, "System default"

        default = next(
            (
                (index, name)
                for index, name in devices
                if "default output" in name.casefold()
            ),
            devices[0] if devices else None,
        )
        if default is None:
            raise ValueError(f"No {device_type} devices were found.")
        return default[0], default[1]

    if not devices:
        raise ValueError(f"No {device_type} devices were found.")

    matches: list[tuple[int, str]] = []
    saved_index = None
    saved_label = value
    if ":" in value:
        prefix, suffix = value.split(":", 1)
        if prefix.strip().isdecimal():
            saved_index = int(prefix.strip())
            saved_label = suffix.strip()

    if value.isdecimal() or saved_index is not None:
        device_index = int(value) if value.isdecimal() else saved_index
        matches = [item for item in devices if item[0] == device_index]
    else:
        needle = saved_label.casefold()
        matches = [
            item for item in devices
            if needle == item[1].casefold() or needle in item[1].casefold()
        ]

    if not matches:
        available = "; ".join(f"{index}: {name}" for index, name in devices)
        raise ValueError(
            f"Could not find {device_type} device {value!r}. Available devices: {available}"
        )
    if len(matches) > 1:
        available = "; ".join(f"{index}: {name}" for index, name in matches)
        raise ValueError(
            f"{device_type.title()} device name {value!r} is ambiguous. "
            f"Use its numeric ID: {available}"
        )
    return matches[0]


def build_headless_plan(
    settings: dict[str, Any],
    args: argparse.Namespace,
    auth_token: str,
    *,
    input_devices: list[tuple[int, str]] | None = None,
    loopback_devices: list[tuple[int, str]] | None = None,
) -> dict[str, Any]:
    """Build the exact transmitter arguments from saved settings and CLI overrides."""
    mode = str(args.mode or settings.get("mode") or "AM").strip().upper()
    if mode not in SUPPORTED_MODES:
        raise ValueError(f"Unsupported transmission mode: {mode}.")

    source_labels = {
        "microphone / line input": "mic",
        "microphone": "mic",
        "desktop audio (output loopback)": "loopback",
        "desktop audio": "loopback",
        "audio playlist": "playlist",
        "mic": "mic",
        "loopback": "loopback",
        "playlist": "playlist",
    }
    saved_source = _setting_text(settings, "audio_source", "Microphone / line input")
    audio_source = args.audio_source or source_labels.get(saved_source.casefold(), "mic")

    if mode == "CW":
        audio_source = "mic"
    if mode == "DRM" and audio_source == "loopback":
        raise ValueError("DRM mode supports microphone and playlist inputs, not desktop audio loopback.")

    selected_device: int | None = None
    selected_device_name = (
        "Not used"
        if mode == "CW" or audio_source == "playlist"
        else "System default"
    )
    device_request = args.audio_device or _setting_text(settings, "audio_device", "System default")

    if mode != "CW" and audio_source == "mic":
        if str(device_request).strip().casefold() not in {"", "default", "system default"}:
            available = input_devices if input_devices is not None else list_audio_inputs()
            selected_device, selected_device_name = _resolve_device(
                available,
                str(device_request),
                device_type="audio input",
                allow_default=True,
            )
    elif mode != "CW" and audio_source == "loopback":
        available_loopback = (
            loopback_devices
            if loopback_devices is not None
            else list_loopback_devices()
        )
        selected_device, selected_device_name = _resolve_device(
            available_loopback,
            str(device_request) if device_request else None,
            device_type="desktop loopback",
            allow_default=False,
        )

    saved_sources = settings.get("playlist_sources")
    if not isinstance(saved_sources, list):
        saved_sources = settings.get("playlist_files", [])
    if not isinstance(saved_sources, list):
        saved_sources = []

    playlist_sources = args.playlist if args.playlist is not None else saved_sources
    normalized_sources = normalize_audio_sources(playlist_sources)

    excluded = settings.get("playlist_excluded_files", [])
    if not isinstance(excluded, list):
        excluded = []
    playlist_files, _signature = build_playlist_snapshot(normalized_sources, excluded)

    if audio_source == "playlist" and not playlist_files:
        raise ValueError(
            "The playlist source is selected, but no supported audio files were found. "
            "Add files with --playlist or update playlist_sources in the settings file."
        )

    allow_overlap = (
        args.allow_overlap
        if args.allow_overlap is not None
        else _setting_bool(settings, "allow_overlap", False)
    )
    playlist_shuffle = (
        args.shuffle
        if args.shuffle is not None
        else _setting_bool(settings, "playlist_shuffle", False)
    )
    playlist_loop = (
        args.loop
        if args.loop is not None
        else _setting_bool(settings, "playlist_loop", False)
    )

    selected_frequency = (
        args.frequency
        if args.frequency is not None
        else _setting_text(settings, "frequency", "7.250 MHz")
    )
    raw_fallbacks = settings.get("frequency_fallbacks", [])
    if not isinstance(raw_fallbacks, list):
        raw_fallbacks = []
    frequency_candidates = build_frequency_candidates(
        selected_frequency,
        raw_fallbacks,
    )

    config = build_transmit_config(
        station_name=args.station_name or _setting_text(settings, "station_name", ""),
        description=args.description if args.description is not None else _setting_text(settings, "description", ""),
        frequency=selected_frequency,
        mode=mode,
        station_dbm=args.power if args.power is not None else _setting_text(settings, "station_dbm", "50"),
        allow_overlap=allow_overlap,
        auth_token=auth_token,
        cw_text=_setting_text(settings, "cw_text", "CQ CQ DE TXITPY"),
        cw_wpm=_setting_text(settings, "cw_wpm", "18"),
        cw_loop=_setting_bool(settings, "cw_loop", True),
        station_logo_url=_setting_text(settings, "station_logo_url", ""),
        station_details=_setting_text(settings, "station_details", ""),
        station_page_layout=_setting_text(settings, "station_page_layout", ""),
        fade_enabled=_setting_bool(settings, "fade_enabled", False),
        fade_seconds=_setting_text(settings, "fade_seconds", "15"),
        fade_min_dbm=_setting_text(settings, "fade_min_dbm", "25"),
        fade_shape=_setting_text(settings, "fade_shape", "0").split("-", 1)[0].strip(),
        agc_enabled=_setting_bool(settings, "agc_enabled", True),
        agc_target=_setting_text(settings, "agc_target", "0.19"),
        mic_gain_db=_setting_text(settings, "mic_gain_db", "0"),
        bass_db=_setting_text(settings, "bass_db", "0"),
        treble_db=_setting_text(settings, "treble_db", "0"),
        presence_db=_setting_text(settings, "presence_db", "0"),
        drive=_setting_text(settings, "drive", "0.015"),
        ssb_bandwidth_hz=_setting_text(settings, "ssb_bandwidth_hz", "10000"),
        ssb_low_cut_hz=_setting_text(settings, "ssb_low_cut_hz", "325"),
        ssb_high_cut_hz=_setting_text(settings, "ssb_high_cut_hz", "0"),
        am_bandwidth_hz=_setting_text(settings, "am_bandwidth_hz", "12000"),
        am_pre_emphasis=_setting_bool(settings, "am_pre_emphasis", False),
        drm_service_label=_setting_text(settings, "drm_service_label", ""),
        drm_service_id=_setting_text(settings, "drm_service_id", "0xB30001"),
        drm_language=_setting_text(settings, "drm_language", "English"),
        drm_programme_type=_setting_text(settings, "drm_programme_type", "No programme type"),
        drm_iso_language=_setting_text(settings, "drm_iso_language", ""),
        drm_iso_country=_setting_text(settings, "drm_iso_country", ""),
        drm_audio_codec=_setting_text(settings, "drm_audio_codec", "xHE-AAC"),
        drm_msc_mode=_setting_text(settings, "drm_msc_mode", "16-QAM"),
        drm_interleaving=_setting_text(settings, "drm_interleaving", "Long (2 s)").split(" ", 1)[0].casefold(),
    )

    config["rfHz"] = frequency_candidates[0][1]
    return {
        "config": config,
        "base_config": dict(config),
        "frequency_candidates": frequency_candidates,
        "input_device": selected_device,
        "audio_device_name": selected_device_name,
        "audio_source": audio_source,
        "playlist_files": playlist_files if audio_source == "playlist" else [],
        "playlist_shuffle": playlist_shuffle,
        "playlist_loop": playlist_loop,
    }


def _read_auth_token(args: argparse.Namespace) -> str:
    token_file = args.auth_token_file.expanduser() if args.auth_token_file is not None else auth_token_path()
    if args.auth_token_file is not None or token_file.is_file():
        try:
            token = token_file.read_text(encoding="utf-8-sig").strip()
        except OSError as exc:
            raise ValueError(f"Could not read the TXITY session token file {token_file}: {exc}") from exc
        source = f"the file {token_file}"
    else:
        token = os.environ.get(args.auth_token_env, "").strip()
        source = (
            f"the default token file {token_file} or environment variable {args.auth_token_env}"
        )

    if not is_auth_token(token):
        raise ValueError(
            f"No valid TXITY Discord session token was found in {source}. "
            "Sign in with Discord in TXITPY or set the token environment variable."
        )
    return token


def _print_devices(devices: list[tuple[int, str]]) -> None:
    for index, name in devices:
        print(f"{index}: {name}")


def _handle_event(
    event: dict[str, Any],
    *,
    logger: logging.Logger,
    transmitter: Any,
    shutdown_event: threading.Event,
    state: dict[str, Any],
    plan: dict[str, Any],
) -> None:
    kind = event.get("kind")

    if kind == "connection":
        logger.info("%s", safe_error_text(str(event.get("text") or ""), 400))
    elif kind == "socket_error":
        logger.warning("TXITY status connection: %s", safe_error_text(str(event.get("text") or ""), 800))
    elif kind == "tx_state":
        status = str(event.get("state") or "")
        logger.info("%s", safe_error_text(str(event.get("text") or status), 400))
        if status == "starting":
            state["negative_reason"] = None
            state["negative_count"] = 0
            state["ptt_confirmed"] = False
        elif status == "stopped":
            if state.get("retry_pending"):
                candidate_index = int(state["retry_candidate_index"])
                candidates = plan["frequency_candidates"]
                display_frequency, frequency_hz = candidates[candidate_index]
                retry_config = dict(plan["base_config"])
                retry_config["rfHz"] = frequency_hz
                plan["config"] = retry_config
                state["frequency_index"] = candidate_index
                state["retry_pending"] = False
                state["negative_reason"] = None
                state["negative_count"] = 0
                state["ptt_confirmed"] = False
                logger.info(
                    "Starting alternate frequency %d of %d: %s.",
                    candidate_index + 1,
                    len(candidates),
                    display_frequency,
                )
                try:
                    transmitter.start(
                        plan["config"],
                        input_device=plan["input_device"],
                        audio_source=plan["audio_source"],
                        playlist_files=plan["playlist_files"],
                        playlist_shuffle=plan["playlist_shuffle"],
                        playlist_loop=plan["playlist_loop"],
                    )
                except Exception as exc:
                    logger.error(
                        "Could not start alternate frequency %s: %s",
                        display_frequency,
                        safe_error_text(str(exc), 800),
                    )
                    state["exit_code"] = 1
                    shutdown_event.set()
            else:
                shutdown_event.set()
    elif kind == "tx_error":
        logger.error("Transmission failed: %s", safe_error_text(str(event.get("text") or ""), 800))
        state["exit_code"] = 1
        shutdown_event.set()
        transmitter.stop()
    elif kind == "tx_audio_warning":
        logger.warning("%s", safe_error_text(str(event.get("text") or ""), 400))
    elif kind == "tx_server_message":
        data = event.get("data")
        if isinstance(data, dict):
            message_type = str(data.get("type") or "")
            message = safe_error_text(str(data.get("message") or data.get("reason") or message_type), 500)
            logger.warning("TXITY: %s", message)
            if message_type in {"kicked", "banned"}:
                state["exit_code"] = 1
                shutdown_event.set()
                transmitter.stop()
    elif kind == "status":
        data = event.get("data")
        if not isinstance(data, dict) or not transmitter.is_busy:
            return

        if data.get("ptt") is True:
            if not state["ptt_confirmed"]:
                logger.info("TXITY confirmed PTT.")
            state["ptt_confirmed"] = True
            state["negative_reason"] = None
            state["negative_count"] = 0
            return

        if data.get("ptt") is not False:
            return

        queue_position = data.get("queuePosition")
        if isinstance(queue_position, int) and queue_position >= 0:
            logger.info("Waiting in TXITY transmitter queue (position %d).", queue_position + 1)
            state["negative_reason"] = None
            state["negative_count"] = 0
            return

        if data.get("txStartUsable") is False or data.get("txBlockReason") not in (None, False, ""):
            reason = data.get("txStartReason", data.get("txBlockReason"))
            reason_key = str(reason if reason is not None else "txStartUsable=false")
            reasons = {
                "2": "transmitter capacity is currently full",
                "3": "frequency sharing must be enabled by both stations",
                "4": "the total transmitter limit has been reached",
                "5": "a station name is required",
            }
            if transmitter.server_rejection_ready:
                if reason_key == state["negative_reason"]:
                    state["negative_count"] += 1
                else:
                    state["negative_reason"] = reason_key
                    state["negative_count"] = 1

                if state["negative_count"] >= 2:
                    reason_text = reasons.get(reason_key, "TXITY rejected the transmission request")
                    candidates = plan.get("frequency_candidates", [])
                    current_index = int(state.get("frequency_index", 0))
                    can_retry = (
                        reason_key == "3"
                        and not state["ptt_confirmed"]
                        and current_index + 1 < len(candidates)
                    )
                    if can_retry:
                        next_index = current_index + 1
                        display_frequency = candidates[next_index][0]
                        logger.warning(
                            "TXITY denied frequency %s. Trying alternate frequency %d of %d: %s.",
                            candidates[current_index][0],
                            next_index + 1,
                            len(candidates),
                            display_frequency,
                        )
                        state["retry_candidate_index"] = next_index
                        state["retry_pending"] = True
                        transmitter.stop()
                    else:
                        if reason_key == "3" and len(candidates) > 1:
                            logger.error(
                                "TXITY rejected all %d configured frequencies. %s (reason %s).",
                                len(candidates),
                                reason_text,
                                reason_key,
                            )
                        else:
                            logger.error("TXITY did not accept PTT: %s (reason %s).", reason_text, reason_key)
                        state["exit_code"] = 1
                        shutdown_event.set()
                        transmitter.stop()
            else:
                logger.info("Waiting for TXITY to confirm the PTT request.")


def run_headless(
    args: argparse.Namespace,
    *,
    client_factory: Callable[[], Any],
    transmitter_factory: Callable[[Any, Any], Any],
    default_settings_path: Path,
) -> int:
    logger = logging.getLogger("TXITPY")

    try:
        logger = _configure_logging(args)

        if args.list_inputs:
            _print_devices(list_audio_inputs())
            return 0
        if args.list_loopback_devices:
            _print_devices(list_loopback_devices())
            return 0

        settings_path = args.config.expanduser() if args.config is not None else default_settings_path
        settings = load_settings(settings_path)
        token = _read_auth_token(args)
        plan = build_headless_plan(settings, args, token)

        logger.info("Using settings file: %s", settings_path)
        logger.info(
            "Starting %s on %s using %s input.",
            plan["config"].get("_txitpyMode", plan["config"]["mode"]),
            plan["config"]["rfHz"],
            plan["audio_source"],
        )
        logger.info("Audio device: %s", plan["audio_device_name"])

        shutdown_event = threading.Event()
        old_handlers: dict[int, Any] = {}

        def request_shutdown(_signum: int, _frame: Any) -> None:
            shutdown_event.set()

        watched_signals = [signal.SIGINT, signal.SIGTERM]
        if hasattr(signal, "SIGBREAK"):
            watched_signals.append(signal.SIGBREAK)
        for signum in watched_signals:
            try:
                old_handlers[signum] = signal.signal(signum, request_shutdown)
            except (OSError, ValueError):
                pass

        client = None
        transmitter = None
        state = {
            "exit_code": 0,
            "negative_reason": None,
            "negative_count": 0,
            "ptt_confirmed": False,
            "frequency_index": 0,
            "retry_pending": False,
            "retry_candidate_index": 0,
        }

        try:
            client = client_factory()
            transmitter = transmitter_factory(client, client.events)
            client.set_discord_auth(token, None)
            client.connect()

            connect_deadline = time.monotonic() + max(1.0, args.connect_timeout)
            connected = False
            while not shutdown_event.is_set():
                if client.control_connected():
                    connected = True
                    break
                if time.monotonic() >= connect_deadline:
                    raise TimeoutError(
                        "The authenticated TXITY connection was not ready before --connect-timeout expired."
                    )
                try:
                    event = client.events.get(timeout=0.25)
                except queue.Empty:
                    continue
                if event.get("kind") == "connection":
                    logger.info("%s", safe_error_text(str(event.get("text") or ""), 400))
                elif event.get("kind") == "socket_error":
                    logger.warning(
                        "TXITY status connection: %s",
                        safe_error_text(str(event.get("text") or ""), 800),
                    )
                elif event.get("kind") == "tx_server_message":
                    data = event.get("data")
                    if isinstance(data, dict):
                        message_type = str(data.get("type") or "")
                        message = safe_error_text(
                            str(data.get("message") or data.get("reason") or message_type),
                            500,
                        )
                        logger.warning("TXITY: %s", message)
                        if message_type in {"kicked", "banned"}:
                            raise RuntimeError(f"TXITY refused the session: {message}")

            if shutdown_event.is_set():
                return 0
            if not connected:
                raise ConnectionError("TXITPY stopped before the TXITY connection became ready.")

            transmitter.start(
                plan["config"],
                input_device=plan["input_device"],
                audio_source=plan["audio_source"],
                playlist_files=plan["playlist_files"],
                playlist_shuffle=plan["playlist_shuffle"],
                playlist_loop=plan["playlist_loop"],
            )
            logger.info("Transmission started. Press Ctrl+C to stop.")

            while not shutdown_event.is_set():
                try:
                    event = client.events.get(timeout=0.5)
                except queue.Empty:
                    continue
                _handle_event(
                    event,
                    logger=logger,
                    transmitter=transmitter,
                    shutdown_event=shutdown_event,
                    state=state,
                    plan=plan,
                )

            return int(state["exit_code"])
        finally:
            if transmitter is not None:
                transmitter.stop(wait=5.0)
            if client is not None:
                client.disconnect()
            for signum, previous in old_handlers.items():
                try:
                    signal.signal(signum, previous)
                except (OSError, ValueError):
                    pass

    except Exception as exc:
        logger.error("%s", safe_error_text(str(exc), 1_200))
        return 2
