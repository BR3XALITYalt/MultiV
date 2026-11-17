# TXITPY

TXITPY connects to TXITY. Sign in with Discord to browse stations, configure your station, and stream audio to the virtual service.

## Install

Install Python 3.12 or newer. Windows also needs the Microsoft Edge WebView2 Runtime.

Open a terminal in the TXITPY folder:

```bash
python -m pip install -r requirements.txt
python main.py
```

Sign in with Discord when TXITPY opens. TXITY must enable secure Discord sign-in.

## Transmit

Open **Transmit**. Enter your station name, frequency, and signal type. The Start/Stop button stays in the page header.

Choose an audio source:

- **Microphone:** Select an input device.
- **Desktop audio:** Select a Windows playback device.
- **Playlist:** Add audio files or folders.

TXITPY supports MP3, WAV, FLAC, OGG, Opus, M4A, and AIFF. It decodes each track and converts it to 48 kHz mono audio before transmission. Use **Shuffle playlist** or **Loop playlist** beside the playlist. TXITPY refreshes imported folders when their contents change. Playlist edits take effect when you start playback again.

Open **Advanced settings** for signal fading, server AGC, microphone gain, tone controls, compression, and bandwidth filters. These controls start collapsed.

TXITPY reconnects when its control or audio connection drops. It restores the station configuration and PTT state after reconnecting. It keeps retrying until you stop the transmission. TXITPY also keeps Windows awake during a transmission. When microphone or desktop audio pauses, it keeps the connection open and sends silence until audio returns.

## Signal modes

### NFM

TXITPY generates NFM with a 6 kHz intermediate frequency, 2.5 kHz peak deviation, and a 30-3,000 Hz audio band. The estimated Carson-rule bandwidth is 11 kHz.

The **NFM transport bandwidth** control requests a TXITY USB/LSB passband from 1 to 20 kHz. It defaults to 12 kHz. This control changes the transport passband, not FM deviation or the audio filter. A narrower passband can clip the signal.

TXITY does not offer native NFM. TXITPY sends its generated FM signal through the existing USB or LSB transport. The frequency you enter is the NFM channel center; TXITY's station directory shows the underlying sideband transport with its carrier offset by 6 kHz.

NFM currently works only in TXITY's 7.000-8.000 MHz room. An independent receiver test has not verified this signal path. TXITY filtering and receiver settings can affect the result.

### AM (DEV)

**AM (DEV)** is a diagnostic mode. It opens two TXITY sessions under the same Discord account at the same frequency. One session uses USB+C and the other uses LSB+C. TXITPY enables frequency overlap for both sessions and sends each the same 512-sample, 48 kHz Float32 audio frame.

TXITY may reject the second session. Its server generates each carrier independently, so the two carriers may not share phase or amplitude. A successful connection does not guarantee a correctly reconstructed AM signal.

### DRM [EXPERIMENTAL]

DRM builds one mono xHE-AAC programme in a 4.5 kHz DRM30 channel. Choose 16-QAM or 64-QAM for MSC modulation; SDC uses 16-QAM. The default 16-QAM setting improves robustness and carries less audio data. Use 64-QAM when reception is strong and you need more audio capacity.

Long MSC interleaving (2 seconds) improves resistance to fading. Short interleaving (400 ms) reduces delay. Choose xHE-AAC or legacy HE-AAC for audio. Choose HE-AAC when your receiver does not support xHE-AAC.

DRM supports microphone input and OGG, MP3, or FLAC playlists. It does not support desktop-audio loopback. WAV and AIFF playlist entries do not play in DRM mode.

When you select DRM, TXITPY checks for its encoder and asks before installing missing dependencies. On Windows, setup can install the official Rust toolchain, fetch DecDRM 0.7.1 and its codec dependencies, and build the encoder. TXITPY verifies the Rust installer checksum before running it. The source build needs Git, CMake, and Microsoft C++ Build Tools with the **Desktop development with C++** workload. Setup may ask for administrator approval. Other platforms need Rust and a working C/C++ compiler.

TXITY does not offer native DRM. TXITPY sends the generated DRM waveform through TXITY's DSB audio transport. An independent DRM receiver test has not verified this path.

### CW

Choose **CW** as the signal type. Enter a message and speed in the **CW message** section. Select **Repeat CW message** to send it continuously.

## Station profile and browser control

Open **Settings** to set your station logo URL and station page details. TXITPY sends these values to TXITY as `stationLogoUrl` and `stationDetails`. Station page details support up to 1,200 characters.

### Browser control panel

Set a password of at least 12 characters in **Settings**, then enable **Web GUI**. TXITPY listens on `0.0.0.0:23234`.

Open the panel on this computer:

```text
http://127.0.0.1:23234
```

Other devices on your local network can use this computer's LAN IP and port `23234`. The panel follows TXITY's transmit and station-profile layout. It can update station settings and request transmission start or stop. Select audio devices and playlist files in the desktop app.

TXITPY stores a salted PBKDF2 password hash and keeps authenticated browser sessions in memory. The panel uses HTTP, not HTTPS. Use it on a trusted local network. Do not expose port `23234` to the public internet or forward it through your router. Keep TXITPY running while you use the panel.

## Stations

Open **Stations** to browse TXITY's station directory. Select a station to view its profile and comments.

## Discord and settings

TXITPY keeps your Discord session between launches. Select **Disconnect Discord** to clear the saved browser session and remove the saved token.

TXITPY stores station and transmission preferences here:

- **Windows:** `%APPDATA%\\TXITPY\\transmit-settings.json`
- **Linux and macOS:** `$XDG_CONFIG_HOME/TXITPY/transmit-settings.json`, or `~/.config/TXITPY/transmit-settings.json`

On Windows, TXITPY stores the Discord session token separately at `%PROGRAMDATA%\\TXITPY\\token.txt`. On Linux and macOS, it uses the application configuration directory.

## Command-line and service mode

Run `python main.py --help` to see all command-line options.

Use `--no-gui` (or `--nogui`) to start a transmission from saved settings without opening the desktop window. Options such as `--station-name`, `--frequency`, `--mode`, `--power`, `--audio-source`, `--audio-device`, and `--playlist` override saved values.

The headless client reads the saved token automatically. Use `--auth-token-file` to choose another token file, or set `TXITPY_DISCORD_AUTH_TOKEN` to provide the token through an environment variable.

Examples:

```powershell
# List microphone and line-input devices
python main.py --list-inputs

# Start with saved settings
python main.py --no-gui

# Override the mode and play a folder
python main.py --no-gui --mode DRM --audio-source playlist --playlist "D:\Radio\Playlist"
```
