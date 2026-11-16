# TXITPY

TXITPY is a Python desktop client for TXITY's simulated radio-broadcast service. It uses Tkinter and ttk for the interface and TXITY's existing Discord sign-in, control WebSocket, and audio WebSocket.

## Features

TXITPY is the desktop app. TXITY refers to the website and simulated service that TXITPY connects to.

- Live server status and a searchable station directory with station profiles, supplied images, custom page blocks, and comments.
- Discord sign-in through TXITY's sign-in page in an embedded WebView2 window.
- Live configuration and push-to-talk control through TXITY's authenticated control WebSocket.
- Microphone or Windows desktop-audio input sent as mono Float32 PCM at 48 kHz in 512-sample frames.
- Windows WASAPI loopback capture. Stereo playback is downmixed to mono and resampled to 48 kHz.
- Audio playlists with multi-file and recursive folder import, MP3/WAV/FLAC/OGG/AIFF playback, shuffle, and loop controls. The full playlist is decoded before transmission into a temporary disk-backed PCM cache, then a prefilled playback queue feeds the stream without decoding during playback. Imported folders refresh automatically when tracks are added, removed, renamed, or edited; changes during playback apply to the next start.
- CW control mode without an audio stream.
- Signal fading, AGC, microphone gain, tone controls, SSB bandwidth and cuts, and AM bandwidth and pre-emphasis.
- Local autosave for station identity and transmit preferences.
- On Windows, minimizing the window hides TXITPY in the system tray. Left-click the tray icon to restore it, or right-click for Open and Quit actions.

TXITPY sends microphone and desktop-loopback audio to TXITY's virtual service. It does not control physical radio hardware.

## Sign-in and session handling

Discord sign-in is required before the dashboard opens. At startup, TXITPY checks its app-specific WebView2 profile in a hidden browser window. A valid TXITY session opens the dashboard; otherwise, TXITPY shows the sign-in page.

The browser profile keeps TXITY's website session between launches. TXITPY reads the session token from TXITY's own origin and keeps it in memory. It does not write the token to the settings file or application logs. Disconnecting Discord clears the saved browser profile.

## Interface

The app uses native Tkinter and ttk widgets, a horizontal navigation bar, rectangular panels, and Segoe UI. Its light and dark themes use Material 3 surface roles and blue and cyan accents adapted from [TeleDrive](https://github.com/Mahmud0808/TeleDrive). The light primary blue is darkened to meet WCAG AA contrast for text. The toolbar switches between themes.

## Requirements

- Python 3.12 or newer
- Microsoft Edge WebView2 Runtime on Windows
- pystray for the Windows system tray integration (installed by the Windows requirements)
- PyAudioWPatch and a Windows WASAPI playback device for desktop-audio loopback

Install the dependencies:

```bash
python -m pip install -r requirements.txt
```

Run the app:

```bash
python main.py
```

## Local settings

On Windows, TXITPY stores station and transmit preferences in `%APPDATA%\TXITPY\transmit-settings.json`. On Linux and macOS, it uses `$XDG_CONFIG_HOME/TXITPY/transmit-settings.json` or `~/.config/TXITPY/transmit-settings.json`.

The settings file does not contain Discord session tokens.

## Tests

```bash
python -m unittest discover -s tests -v
```

## TXITY data

TXITPY reads public server configuration from `/api/config` and the station directory from `/users.json` on `https://txity.radiodiscussion.org`. The listing supplies client counts, station metadata, profile fields, gallery images, custom page blocks, and recent comments. TXITPY loads station images from TXITY's media host and Discord's image CDN.

The Stations page shows refresh results and fetch errors. If TXITY changes its JSON schema or status protocol, the client may need an update. Discord sign-in is available only when TXITY reports `secureDiscordAuth: true`.

TXITY returns the signed Discord session to its homepage. The embedded browser waits for TXITY's frontend to store the session in same-origin browser storage. TXITPY reads the token only on `https://txity.radiodiscussion.org` and sends it through a temporary loopback callback. The callback listens on loopback, uses a single-use random state value, expires after five minutes, and closes after use.

Playlist audio is fully decoded to 48 kHz mono Float32 PCM before PTT begins. TXITPY stores that prepared audio in a temporary disk-backed cache, fills a larger playback queue, and reuses the cache for shuffle and loop playback. This keeps file decoding out of the live playback path without loading an entire playlist into RAM. The temporary cache is closed when transmission ends. TXITPY watches imported folders in the background and refreshes the saved track list when file paths or file metadata change. Track progress follows audio sent to the service, not the decoder's read-ahead position.
