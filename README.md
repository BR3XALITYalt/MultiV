# TXITPY

Use TXITPY with TXITY, a simulated radio service. Sign in with Discord to browse virtual stations, configure your station, and send audio to the service.

## Features

- Browse station listings, profiles, images, and comments.
- Send microphone audio, Windows desktop audio, or playlist audio.
- Play MP3, WAV, FLAC, OGG, and AIFF files with shuffle and loop options.
- Send a configured Morse-code message in CW mode.
- Adjust AGC, input gain, tone, and mode-specific bandwidth settings.
- Save station details and transmission preferences between launches.
- Minimize the TXITPY window to the Windows system tray and select its icon to restore it.

Use TXITPY with TXITY's virtual service. Do not connect the app to physical radio equipment.

## Requirements

- Python 3.12 or newer.
- Microsoft Edge WebView2 Runtime on Windows.
- A microphone or supported audio files for audio input.
- A Windows playback device for desktop-audio capture.

## Install and run

Open a terminal in the TXITPY folder and run:

```bash
python -m pip install -r requirements.txt
python main.py
```

On your first launch, sign in with Discord. TXITY must have secure Discord sign-in enabled.

## Send audio

Open **Transmit**. Enter your station name, frequency, and mode, then choose an audio source:

- **Microphone:** Choose an available microphone or input device.
- **Desktop audio:** On Windows, choose the playback device whose sound you want to capture.
- **Audio playlist:** Add audio files or folders. Select **Shuffle playlist** or **Loop playlist** to change playback.

Select **Start Transmission** to connect to TXITY. Select **Stop Transmission** when you finish.

### Playlist playback

Add MP3, WAV, FLAC, OGG, or AIFF files. Add a folder to include its tracks and keep the playlist in sync with changes on disk. TXITPY refreshes imported folders when you add, remove, rename, or edit files. Changes take effect the next time you start playback.

TXITPY reads playlist audio ahead while playback runs. Before transmission starts, it fills about 2.7 seconds of audio. During playback, it keeps up to about 5.5 seconds queued. This buffer gives the decoder time to read upcoming audio and helps prevent gaps.

### CW mode

Choose **CW** as the transmission mode. Enter your message and speed in the **CW message** section. Select **Repeat CW message** to send it continuously.

## Browse stations

Open **Stations** to browse the station directory. Select a station card to view its profile, station details, and comments.

## Sign-in and saved settings

Keep Discord connected to reuse your TXITY sign-in between launches. Select **Disconnect Discord** in TXITPY to clear the saved browser session.

Use the following paths to find your station and transmission preferences:

- **Windows:** `%APPDATA%\TXITPY\transmit-settings.json`
- **Linux and macOS:** `$XDG_CONFIG_HOME/TXITPY/transmit-settings.json`, or `~/.config/TXITPY/transmit-settings.json` when `XDG_CONFIG_HOME` is not set.

The preferences file does not contain Discord session tokens.
