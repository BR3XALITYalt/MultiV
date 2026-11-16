# TXITPY

TXITPY connects to TXITY, a simulated radio service. Sign in with Discord to browse virtual stations, configure your station, and send audio to TXITY.

## Features

- Browse stations, profiles, images, and comments.
- Send microphone audio, Windows desktop audio, or a playlist.
- Play MP3, WAV, FLAC, OGG, and AIFF files with shuffle and loop options.
- Use CW mode to send a configured Morse-code message.
- Adjust audio processing, including AGC, gain, tone controls, and mode-specific bandwidth settings.
- Save station details and transmission preferences between launches.
- Minimize TXITPY to the Windows system tray and restore it from the tray icon.

TXITPY sends audio to TXITY's virtual service. It does not control physical radio equipment.

## Requirements

- Python 3.12 or newer.
- Microsoft Edge WebView2 Runtime on Windows.
- A microphone for microphone input.
- A Windows playback device for desktop-audio capture.


## Install and run

Open a terminal in the TXITPY folder and run:

```bash
python -m pip install -r requirements.txt
python main.py
```

On first launch, sign in with Discord through TXITY. TXITY must have secure Discord sign-in enabled.

## Send audio

Open the **Transmit** page and enter your station details, frequency, and mode. Choose an audio source:

- **Microphone:** Select an available microphone or input device.
- **Desktop audio:** On Windows, select the playback device whose sound TXITPY should capture.
- **Audio playlist:** Add audio files or folders. Use **Shuffle playlist** and **Loop playlist** to change playback.

Select **Start Transmission** to connect to TXITY. Select **Stop Transmission** to end it.

### Playlist playback

TXITPY supports MP3, WAV, FLAC, OGG, and AIFF files. Add a folder to keep its track list in sync with changes on disk. TXITPY refreshes the list when files are added, removed, renamed, or edited. Changes take effect the next time you start playback.

TXITPY decodes playlist audio in the background while playback runs. It keeps up to about 5.5 seconds of audio queued and fills about 2.7 seconds before transmission starts. The queue pauses when full, so TXITPY does not load the whole playlist into memory.

### CW mode

Choose **CW** as the transmission mode and enter the message and speed in the CW message section. Use **Repeat CW message** to keep the message running.

## Browse stations

Open **Stations** to view the station directory. Select a station card to see its profile, station details, and comments.

## Sign-in and saved settings

TXITPY remembers your TXITY sign-in between launches. Disconnect Discord in TXITPY to clear the saved session. The preferences file does not contain Discord session tokens.

TXITPY stores station and transmission preferences at:

- **Windows:** `%APPDATA%\TXITPY\transmit-settings.json`
- **Linux and macOS:** `$XDG_CONFIG_HOME/TXITPY/transmit-settings.json`, or `~/.config/TXITPY/transmit-settings.json` when `XDG_CONFIG_HOME` is not set.
