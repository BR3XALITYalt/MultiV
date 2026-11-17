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

## Command-line and service mode

Run `python main.py --help` to see all command-line options.

`--no-gui` (also accepted as `--nogui`) starts a transmission directly from the saved transmit settings. Options such as `--station-name`, `--frequency`, `--mode`, `--power`, `--audio-source`, `--audio-device`, and `--playlist` override individual saved values. Settings not overridden on the command line continue to come from the usual `transmit-settings.json` file.

After Discord sign-in, TXITPY saves the TXITY session token to `%PROGRAMDATA%\TXITPY\token.txt` on Windows so the desktop app and a Windows service can share it (or the app's configuration directory on Linux and macOS). Headless mode reads this saved token automatically. You can override the file with `--auth-token-file` or use `TXITPY_DISCORD_AUTH_TOKEN`. The token stays separate from the transmit settings, and **Disconnect Discord** removes the saved token.

Examples:

```powershell
# List microphone and line-input devices
python main.py --list-inputs

# Start from saved settings without opening a window
python main.py --no-gui

# Override the saved mode and use a playlist
python main.py --no-gui --mode DRM --audio-source playlist --playlist "D:\Radio\Playlist"
```

## Transmit

Open **Transmit** and enter your station name, frequency, and mode. Choose an audio source:

- **Microphone:** Select an input device.
- **Desktop audio:** Select a Windows playback device.
- **Playlist:** Add audio files or folders.

Select **Start Transmission** to begin and **Stop Transmission** to finish. If TXITY's control or audio connection drops during a transmission, TXITPY retries automatically with increasing delays and restores the station configuration and PTT state after reconnecting. It keeps retrying until you select **Stop Transmission**. On Windows, TXITPY keeps the PC awake while a transmission runs. If a microphone or desktop-audio input pauses, TXITPY keeps the connection open until audio returns.

TXITPY supports MP3, WAV, FLAC, OGG, Opus, M4A, and AIFF playlists. During playback, TXITPY decodes tracks and converts them to its 48 kHz mono audio stream. Use **Shuffle playlist** or **Loop playlist** as needed. TXITPY reads audio ahead during playback to reduce gaps. Imported folders refresh when files change; playlist changes take effect the next time you start playback.

### NFM

The **NFM** mode generates narrowband FM in TXITPY with a 5 kHz intermediate frequency, 2 kHz peak deviation, and a 30-2,500 Hz audio band. Its input uses a soft limiter instead of hard clipping to reduce distortion on louder music. It supports microphone, desktop-audio loopback, and Playlist input. TXITY does not provide a native NFM transport mode, so TXITPY sends the generated signal through its existing USB or LSB path. The frequency entered in TXITPY is the resulting NFM channel center. The TXITY station directory displays the underlying sideband transport and its carrier, which is offset by 5 kHz. NFM is currently limited to TXITY's 7.000-8.000 MHz room and has not yet been verified with an independent receiver.

### DRM [EXPERIMENTAL]

The **DRM [EXPERIMENTAL]** mode builds a single mono xHE-AAC programme in a 4.5 kHz DRM30 channel using configurable 16-QAM or 64-QAM MSC modulation and 16-QAM SDC modulation. 16-QAM is selected by default for more reliable decoding; it carries four bits per MSC symbol instead of six with 64-QAM, so it reduces available audio capacity. Choose 64-QAM when reception is strong and higher audio capacity matters. Long MSC interleaving (2 seconds) is selected by default to improve resistance to fading. xHE-AAC is designed for good quality at low bit rates; select legacy HE-AAC in the DRM settings when a receiver does not support xHE-AAC. Short interleaving (400 ms) is available for lower delay. It accepts microphone input or OGG, MP3, and FLAC playlists. WAV and AIFF playlist entries are skipped, and desktop-audio loopback is not supported in DRM mode.

When you select DRM, TXITPY checks whether the DRM encoder and its dependencies are available. If setup is needed, it asks before downloading the official Rust installer on Windows, installing the stable Rust toolchain (1.88 or newer), fetching DecDRM 0.7.1 and its codec dependencies, and building the encoder. The Rust installer checksum is verified before it runs. On other platforms, TXITPY opens the official Rust installation page when the toolchain is missing. TXITPY selects DRM after the build succeeds; cancelling or a failed setup returns to the previous mode.

The source build requires Git, CMake, and the Microsoft C++ Build Tools on Windows. If the MSVC toolchain is missing, TXITPY offers to download the signed Visual Studio Build Tools installer and install the **Desktop development with C++** workload. This may request administrator approval. On other platforms, a working C/C++ compiler is required. The source and compiled encoder are cached locally. DecDRM is licensed under GPL-2.0-or-later; see [drm_encoder/NOTICE.md](drm_encoder/NOTICE.md) and [drm_encoder/COPYING](drm_encoder/COPYING). Its codec submodules have separate licensing terms, so do not redistribute a compiled helper without reviewing those terms.

TXITY does not have a native DRM mode. TXITPY carries the generated DRM waveform through TXITY's existing DSB audio transport. This is an experimental software path and has not yet been verified with an independent DRM receiver.

For CW, choose **CW** as the mode and enter a message and speed in the **CW message** section. Select **Repeat CW message** to keep it playing.

## Stations

Open **Stations** to browse the directory. Select a station to view its profile and comments.

## Sign-in and settings

TXITPY keeps your Discord session between launches. Select **Disconnect Discord** to clear it.

TXITPY saves station and transmission preferences here:

- **Windows:** `%APPDATA%\TXITPY\transmit-settings.json`
- **Linux and macOS:** `$XDG_CONFIG_HOME/TXITPY/transmit-settings.json`, or `~/.config/TXITPY/transmit-settings.json`

The Discord session token is stored separately at `%PROGRAMDATA%\TXITPY\token.txt` on Windows. TXITPY removes it when you select **Disconnect Discord**.

On Windows, minimize TXITPY to put it in the system tray. Select the tray icon to reopen the window.
