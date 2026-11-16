# TXITPY

TXITPY connects to TXITY. Sign in with Discord to browse stations, manage your station, and send audio to the virtual service.

## Install

You need Python 3.12 or newer. Windows users also need Microsoft Edge WebView2 Runtime.

Open a terminal in the TXITPY folder:

```bash
python -m pip install -r requirements.txt
python main.py
```

Sign in with Discord when TXITPY opens. TXITY must have secure Discord sign-in enabled.

## Transmit

Open **Transmit** and enter your station name, frequency, and mode. Choose an audio source:

- **Microphone:** Select an input device.
- **Desktop audio:** Select a Windows playback device.
- **Playlist:** Add audio files or folders.

Select **Start Transmission** to begin and **Stop Transmission** to finish. On Windows, TXITPY keeps the PC awake while a transmission runs. If a microphone or desktop-audio input pauses, TXITPY keeps the connection open until audio returns.

TXITPY supports MP3, WAV, FLAC, OGG, and AIFF playlists. Use **Shuffle playlist** or **Loop playlist** as needed. TXITPY reads audio ahead during playback to reduce gaps. Imported folders refresh when files change; playlist changes take effect the next time you start playback.

### DRM30

The **DRM** mode builds a single mono HE-AAC programme in a 4.5 kHz DRM30 channel. It accepts a microphone or OGG, MP3, and FLAC playlists. WAV and AIFF playlist entries are skipped, and desktop-audio loopback is not supported in DRM mode.

The first DRM start builds the encoder from source. It requires Git, Rust 1.88 or newer, CMake, and a working C/C++ compiler toolchain. The source and build are cached locally. The build uses DecDRM 0.7.1 under GPL-2.0-or-later; see [drm_encoder/NOTICE.md](drm_encoder/NOTICE.md) and [drm_encoder/COPYING](drm_encoder/COPYING). Its codec submodules have separate licensing terms, so do not redistribute a compiled helper without reviewing those terms.

TXITY does not have a native DRM mode. TXITPY carries the generated DRM waveform through TXITY's existing DSB audio transport. This is an experimental software path and has not yet been verified with an independent DRM receiver.

For CW, choose **CW** as the mode and enter a message and speed in the **CW message** section. Select **Repeat CW message** to keep it playing.

## Stations

Open **Stations** to browse the directory. Select a station to view its profile and comments.

## Sign-in and settings

TXITPY keeps your Discord session between launches. Select **Disconnect Discord** to clear it.

TXITPY saves station and transmission preferences here:

- **Windows:** `%APPDATA%\TXITPY\transmit-settings.json`
- **Linux and macOS:** `$XDG_CONFIG_HOME/TXITPY/transmit-settings.json`, or `~/.config/TXITPY/transmit-settings.json`

The settings file does not store Discord session tokens.

On Windows, minimize TXITPY to put it in the system tray. Select the tray icon to reopen the window.
