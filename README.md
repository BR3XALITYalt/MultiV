# TXITPY

A Python desktop companion for the TXITY simulated radio-broadcast service, built with the standard-library Tkinter and ttk GUI toolkits.

## Current scope

This release provides a native desktop client for TXITY with the official Discord sign-in flow:

- Live server status and a searchable station directory with image cards and profile detail pages.
- TXITY's official Discord OAuth flow in an embedded browser window, followed by a one-use loopback callback into the desktop app.
- An authenticated control WebSocket for TXITY configuration and push-to-talk messages.
- A separate authenticated audio WebSocket for microphone or desktop-audio transmission using mono Float32 PCM at 48 kHz, sent in 512-sample binary frames.
- Windows WASAPI loopback capture for selecting a speaker, headset, or other playback endpoint as the audio source. Stereo playback is downmixed to mono and resampled to 48 kHz.
- CW mode using TXITY's control protocol without opening an audio stream.

Discord sign-in is required before the dashboard can be used. On startup, TXITPY checks the existing app-specific WebView2 profile in a hidden browser window. If TXITY still has a valid saved session, the dashboard unlocks automatically; otherwise the sign-in page remains open. The browser profile keeps the website's normal session across launches. TXITPY keeps the active token in memory and does not write it to an application settings file or log it. Disconnect clears the saved browser profile. Microphone and desktop-loopback audio are streamed to the TXITY virtual service only; TXITPY does not control physical radio hardware.

The current Transmit page supports microphone audio in AM, USB, LSB, USB+C, LSB+C, and DSB modes, plus CW control mode. MP3 file playback and the browser's advanced audio processing controls are not yet included.

The desktop interface uses native Tkinter and ttk widgets with horizontal top navigation, square-corner station cards, station profile views with thumbnails, gallery images, custom page blocks and comments, plus a blue Material 3 Expressive-inspired scheme based on TeleDrive's light and dark palettes: blue primary (`#1B6EF3`), cyan tertiary (`#00677E`), and layered Material-style surfaces. A light/dark switch follows the Windows application theme on startup and can be changed from the toolbar. Color values are adapted from [TeleDrive](https://github.com/Mahmud0808/TeleDrive), which uses Kotlin Multiplatform and Compose Material 3 Expressive.

## Requirements

- Python 3.12 or newer
- Microsoft Edge WebView2 Runtime on Windows
- Windows desktop-audio loopback uses PyAudioWPatch and a WASAPI playback device

Install the dependencies:

```bash
python -m pip install -r requirements.txt
```

Run the app:

```bash
python main.py
```

## Tests

```bash
python -m unittest discover -s tests -v
```

## Notes

The app fetches public server configuration and the station directory from `https://txity.radiodiscussion.org`, using `/api/config` and `/users.json`. The JSON listing supplies live client counts and station metadata, including a bounded selection of station profile fields, gallery images, custom page blocks and recent comments. Station images are loaded from TXITY's own media host and Discord's image CDN. Station refresh results and fetch errors are shown directly on the Stations page. If the service changes the JSON schema or status protocol, station extraction or live status may stop working. Discord sign-in is enabled only when TXITY reports `secureDiscordAuth: true`. TXITY currently returns the signed session to its own homepage, so the embedded browser waits for TXITY's frontend to store the session in same-origin browser storage. It reads that value only on `https://txity.radiodiscussion.org` and forwards it through the app's temporary loopback callback. The callback is bound to loopback only, protected by a single-use random state value, expires after five minutes, and shuts down after use.
