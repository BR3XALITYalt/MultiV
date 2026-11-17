# MultiV

MultiV connects to TXITY. Sign in with Discord to browse stations, configure your station, and stream audio to the virtual service.

## Install

Install Python 3.12 or newer. Windows also needs the Microsoft Edge WebView2 Runtime.

Open a terminal in the project folder:

```bash
python -m pip install -r requirements.txt
python main.py
```

Sign in with Discord when MultiV opens. TXITY must enable secure Discord sign-in.

## Transmit

Open **Transmit**. Enter your station name, frequency, and signal type. The Start/Stop button stays in the page header.

Choose an audio source:

- **Microphone:** Select an input device.
- **Desktop audio:** Select a Windows playback device.
- **Playlist:** Add audio files or folders.

MultiV supports MP3, WAV, FLAC, OGG, Opus, M4A, and AIFF. It decodes each track and converts it to 48 kHz mono audio before transmission. Use **Shuffle playlist** or **Loop playlist** beside the playlist. MultiV refreshes imported folders when their contents change. Playlist edits take effect when you start playback again.

Open **Advanced settings** for signal fading, server AGC, microphone gain, tone controls, compression, and bandwidth filters. These controls start collapsed.

MultiV reconnects when its control or audio connection drops. It restores the station configuration and PTT state after reconnecting. It keeps retrying until you stop the transmission. MultiV also keeps Windows awake during a transmission. When microphone or desktop audio pauses, it keeps the connection open and sends silence until audio returns.

## Signal modes

### NFM

MultiV generates NFM with a 5 kHz intermediate frequency, 2.5 kHz peak deviation, and a 30-2,000 Hz audio band. The estimated Carson-rule bandwidth is 9 kHz. The 500 Hz clearance between the IF and the deviation-plus-audio limit keeps the generated spectrum above DC.

The **NFM transport bandwidth** control requests a TXITY USB/LSB passband from 1 to 20 kHz. It defaults to 12 kHz. This control changes the transport passband, not FM deviation or the audio filter. A narrower passband can clip the signal.

TXITY does not offer native NFM. MultiV sends its generated FM signal through the existing USB or LSB transport. To correct the observed passband asymmetry, MultiV moves the NFM IF down by 1 kHz (6 kHz to 5 kHz) and moves the TXITY carrier reference up by 1 kHz. These offsets cancel, so the entered channel center does not change. The station directory shows the underlying sideband transport with its reference 5 kHz below the selected center for USB, or 5 kHz above it for LSB.

NFM runs only in TXITY's 7.000-8.000 MHz room. An independent receiver test has not verified this signal path. TXITY filtering and receiver settings can affect the result.


### FreeDV RADE V1

FreeDV RADE V1 encodes microphone, desktop-audio, or playlist speech using the open-source RADE C modem and LPCNet/FARGAN speech encoder. MultiV uses the stable V1 waveform, not the experimental V2 path.

The generated modem signal is converted to a real 8 kHz audio waveform, resampled to 48 kHz, and sent through TXITY's USB or LSB audio transport. MultiV places the 1.5 kHz RADE audio centre at the frequency you enter. The selected transport carrier is offset by 1.5 kHz to keep the channel centred. The transport passband is set to 3 kHz.

The first start downloads a Windows x64 encoder package from the MultiV release and verifies its SHA-256 checksum before installation. RADE encoding is CPU-intensive. MultiV buffers audio before keying and stops with an error if the encoder cannot keep up. TXITY does not expose a RADE decoder or receive-audio stream to this client, so this mode currently supports transmission only.

Source: [FreeDV RADE](https://freedv.org/radio-autoencoder/) and [RADE C](https://github.com/freedv/rade_c). BSD and Opus notices are included in the runtime package.

### DRM [EXPERIMENTAL]

DRM builds one mono xHE-AAC programme in a 4.5 kHz DRM30 channel. Choose 16-QAM or 64-QAM for MSC modulation; SDC uses 16-QAM. The default 16-QAM setting improves robustness and carries less audio data. Use 64-QAM when reception is strong and you need more audio capacity.

Long MSC interleaving (2 seconds) improves resistance to fading. Short interleaving (400 ms) reduces delay. Choose xHE-AAC or legacy HE-AAC for audio. Choose HE-AAC when your receiver does not support xHE-AAC.

DRM supports microphone input and OGG, MP3, or FLAC playlists. It does not support desktop-audio loopback. WAV and AIFF playlist entries do not play in DRM mode.

When you select DRM, MultiV checks for its encoder and asks before installing missing dependencies. On Windows, setup can install the official Rust toolchain, fetch DecDRM 0.7.1 and its codec dependencies, and build the encoder. MultiV verifies the Rust installer checksum before running it. The source build needs Git, CMake, and Microsoft C++ Build Tools with the **Desktop development with C++** workload. Setup may ask for administrator approval. Other platforms need Rust and a working C/C++ compiler.

TXITY does not offer native DRM. MultiV sends the generated DRM waveform through TXITY's DSB audio transport. An independent DRM receiver test has not verified this path.

### CW

Choose **CW** as the signal type. Enter a message and speed in the **CW message** section. Select **Repeat CW message** to send it continuously.

## Station profile and browser control

Open **Settings** to set your station logo and station page details. Paste an approved image URL into **Station logo URL**, or select **Upload image** to send a PNG, JPEG, WebP, or GIF file up to 5 MB to TXITY. MultiV uses the signed-in Discord session for the upload and fills the URL field with the hosted image URL. MultiV sends the logo URL and station details to TXITY as `stationLogoUrl` and `stationDetails`. Station page details support up to 1,200 characters.

### Browser control panel

Set a password of at least 12 characters in **Settings**, then enable **Web GUI**. MultiV listens on `0.0.0.0:23234`.

Open the panel on this computer:

```text
http://127.0.0.1:23234
```

Other devices on your local network can use this computer's LAN IP and port `23234`. The panel follows TXITY's transmit and station-profile layout. It can update station settings and request transmission start or stop. Select audio devices and playlist files in the desktop app.

MultiV stores a salted PBKDF2 password hash and keeps authenticated browser sessions in memory. The panel uses HTTP, not HTTPS. Use it on a trusted local network. Do not expose port `23234` to the public internet or forward it through your router. Keep MultiV running while you use the panel.

## Stations

Open **Stations** to browse TXITY's station directory. Select a station to view its profile and comments.

## Discord and settings

MultiV keeps your Discord session between launches. Select **Disconnect Discord** to clear the saved browser session and remove the saved token.

MultiV stores station and transmission preferences here:

- **Windows:** `%APPDATA%\\TXITPY\\transmit-settings.json`
- **Linux and macOS:** `$XDG_CONFIG_HOME/TXITPY/transmit-settings.json`, or `~/.config/TXITPY/transmit-settings.json`

On Windows, MultiV stores the Discord session token separately at `%PROGRAMDATA%\\TXITPY\\token.txt`. On Linux and macOS, it uses the application configuration directory.

## SimTX API foundation

MultiV includes an initial SimTX integration module in `simtx_api.py`. It follows the documented SimTX account API and game protocol v25:

- Device-code authorization and rotating access/refresh credentials.
- Online server and persistent-station discovery.
- Single-use, station-bound game-ticket requests.
- TLS connection setup, the v25 protocol preface, protobuf frame encoding/decoding, and the initial audio-transmitter messages.

This is an integration foundation, not a finished SimTX transmitter. The desktop controls do not use it yet. Audio capture, real-time sample pacing, continuous telemetry handling, reconnect behavior, and secure credential-store integration still need to be connected to MultiV's application lifecycle. The existing TXITY client remains unchanged.

The account client keeps credentials in memory. The caller must store refresh credentials in an operating-system credential store; do not put them in settings files or logs.

Documentation: [SimTX API reference](https://wiki.simtx.net/index.php/SimTX_API) · [Device login](https://wiki.simtx.net/index.php/SimTX_API/Device_login) · [Transmitter session](https://wiki.simtx.net/index.php/SimTX_API/Transmitter_session).

## Command-line and service mode

Run `python main.py --help` to see all command-line options.

Use `--no-gui` (or `--nogui`) to start a transmission from saved settings without opening the desktop window. Options such as `--station-name`, `--frequency`, `--mode`, `--power`, `--audio-source`, `--audio-device`, and `--playlist` override saved values.

The headless client reads the saved token on startup. Use `--auth-token-file` to choose another token file, or set `TXITPY_DISCORD_AUTH_TOKEN` to provide the token through an environment variable.

Examples:

```powershell
# List microphone and line-input devices
python main.py --list-inputs

# Start with saved settings
python main.py --no-gui

# Override the mode and play a folder
python main.py --no-gui --mode DRM --audio-source playlist --playlist "D:\Radio\Playlist"
```
