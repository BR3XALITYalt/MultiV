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

Select **Start Transmission** to begin and **Stop Transmission** to finish.

TXITPY supports MP3, WAV, FLAC, OGG, and AIFF playlists. Use **Shuffle playlist** or **Loop playlist** as needed. TXITPY reads audio ahead during playback to reduce gaps. Imported folders refresh when files change; playlist changes take effect the next time you start playback.

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
