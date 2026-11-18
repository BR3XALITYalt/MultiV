![MultiV](assets/banner.svg)

**A multi-source, multi-stream client for virtual radio simulators.**

MultiV lets you select an audio source, configure a radio transmission, and manage your station from one desktop app. Choose a microphone, Windows desktop audio, or a playlist for each transmission. The current transmitter uses one source at a time.

See [ROADMAP.md](ROADMAP.md) for simulator provider support and integration status.

Sign in with Discord to connect to TXITY, browse stations, edit your station profile, and transmit audio.

## Install

Install Python 3.12 or newer. On Windows, install the Microsoft Edge WebView2 Runtime as well.

From the project folder, run:

```bash
python -m pip install -r requirements.txt
python main.py
```

When MultiV opens, sign in with Discord. TXITY must have secure Discord sign-in enabled.

## Transmit

Open **Transmit**. Enter your station name, frequency, and signal type. Use **Start transmission** in the page header to start transmitting. Use the same control to stop.

Choose one audio source:

- **Microphone:** Select an input device.
- **Desktop audio:** Select a Windows playback device.
- **Playlist:** Add audio files or folders.

MultiV supports MP3, WAV, FLAC, OGG, Opus, M4A, and AIFF playlists. It decodes each track before sending the audio to TXITY. Use **Shuffle playlist** and **Loop playlist** beside the playlist controls. MultiV refreshes imported folders when their files change. Playlist edits take effect the next time you start playback.

Open **Advanced settings** to configure signal fading, server AGC, microphone gain, tone controls, compression, and bandwidth filters. This section is collapsed by default.

MultiV reconnects when its control or audio connection drops. After reconnecting, it restores the station configuration and PTT state, then continues trying until you stop the transmission. During transmission, MultiV keeps Windows awake. If microphone or desktop audio pauses, MultiV keeps the audio connection open and sends silence until audio resumes.

## Signal modes

### NFM

MultiV has two NFM profiles:

- **Standard (9 kHz):** 5 kHz IF, 2.5 kHz peak deviation, and a 30-2,000 Hz voice band.
- **Wide (11 kHz):** 6 kHz IF, 2.5 kHz peak deviation, and a 100-3,000 Hz voice band.

Each profile leaves 500 Hz between the IF and the sum of peak deviation and the upper voice-band limit.

TXITY does not provide native NFM. MultiV generates the FM signal and sends it through TXITY's USB or LSB audio transport. The selected IF shifts the transport so the entered frequency remains the channel centre.

The **NFM transport bandwidth** setting requests a TXITY passband from 1 to 20 kHz. The default is 12 kHz. A narrower passband can clip the generated FM signal.

NFM works only in TXITY's 7.000-8.000 MHz room. An independent receiver test has not verified this signal path. TXITY filtering and receiver settings can change the result.

### FreeDV RADE V1

FreeDV RADE V1 encodes speech with the RADE C modem and LPCNet/FARGAN speech encoder. MultiV uses the V1 waveform.

MultiV converts the generated modem signal to a real 8 kHz audio waveform, resamples it to 48 kHz, and sends it through TXITY's USB audio transport. The entered frequency is the USB suppressed-carrier reference. The RADE audio centre is 1.5 kHz, and the transport passband is 3 kHz.

The first time you start RADE, MultiV downloads a Windows x64 encoder package from the MultiV release. It verifies the package's SHA-256 checksum before installation. RADE encoding uses significant CPU time. MultiV buffers audio before keying and stops with an error if the encoder cannot keep up.

TXITY does not provide a RADE decoder or receive-audio stream to MultiV. RADE mode supports transmission only.

References: [FreeDV RADE](https://freedv.org/radio-autoencoder/) and [RADE C](https://github.com/freedv/rade_c). The runtime package includes BSD and Opus notices.

### DRM (experimental)

DRM has three profiles:

- **Robust:** 4.5 kHz occupancy, 16-QAM, mono audio.
- **Quality:** 9 kHz occupancy, 16-QAM, mono audio.
- **Maximum quality:** 9 kHz occupancy, 64-QAM, stereo audio.

The SDC uses 16-QAM in all profiles. 64-QAM and stereo carry more data but require better reception. The Maximum quality profile may perform worse on a weak or fading channel.

Long MSC interleaving adds about 2 seconds of delay and improves resistance to fading. Short interleaving uses about 400 ms and reduces delay. Choose xHE-AAC or HE-AAC. Use HE-AAC if your receiver does not support xHE-AAC. The encoder selects a bitrate that fits the available service capacity.

DRM supports microphone input and OGG, MP3, or FLAC playlists. It does not support desktop-audio loopback. MultiV skips WAV and AIFF playlist entries in DRM mode.

When you select DRM, MultiV checks for the encoder and asks before installing missing dependencies. On Windows, setup can install the official Rust toolchain, download DecDRM 0.7.1 and its codec dependencies, and build the encoder. MultiV checks the Rust installer checksum before running it. The build requires Git, CMake, and Microsoft C++ Build Tools with the **Desktop development with C++** workload. Setup may request administrator approval. On other platforms, install Rust and a working C/C++ compiler.

TXITY does not provide native DRM. MultiV sends the generated DRM waveform through TXITY's DSB audio transport. An independent DRM receiver test has not verified this path.

### CW

Select **CW** as the signal type. Enter the message and speed in **CW message**. Select **Repeat CW message** to send it continuously.

## Station profile

Open **Settings** to edit your station logo and station page details.

To set a station logo, enter a public URL in **Station logo URL** or select **Upload image**. Upload a PNG, JPEG, WebP, or GIF file up to 5 MB. MultiV uses your signed-in Discord session to upload the image and fills in the hosted URL.

Station page details support up to 1,200 characters.

## Browser control panel

In **Settings**, set a password of at least 12 characters and enable **Web GUI**. MultiV listens on port `23234` on all network interfaces.

Open the panel on this computer:

```text
http://127.0.0.1:23234
```

Other devices on your local network can connect to this computer's LAN IP and port `23234`. Use the desktop app to select audio devices and playlist files. The browser panel can edit station settings and start or stop a transmission.

MultiV stores a salted PBKDF2 password hash and keeps authenticated browser sessions in memory. The panel uses HTTP, not HTTPS. Use it only on a trusted local network. Do not expose port `23234` to the public internet or forward it through your router. Keep MultiV running while you use the panel.

## Stations

Open **Stations** to browse the TXITY station directory. Select a station to view its profile and comments.

## Discord and settings

MultiV saves your Discord session between launches. Select **Disconnect Discord** to clear the saved browser session and token.

MultiV stores station and transmission preferences in these locations:

- **Windows:** `%APPDATA%\TXITPY\transmit-settings.json`
- **Linux and macOS:** `$XDG_CONFIG_HOME/TXITPY/transmit-settings.json`, or `~/.config/TXITPY/transmit-settings.json`

On Windows, MultiV stores the Discord session token separately at `%PROGRAMDATA%\TXITPY\token.txt`. On Linux and macOS, it uses the application configuration directory.

## Command line

Run `python main.py --help` to list the command-line options.

Use `--no-gui` (or `--nogui`) to start a transmission from saved settings without opening the desktop window. These options override saved settings:

- `--station-name`
- `--frequency`
- `--mode`
- `--power`
- `--audio-source`
- `--audio-device`
- `--playlist`

The headless client reads the saved token on startup. Use `--auth-token-file` to select another token file, or set `TXITPY_DISCORD_AUTH_TOKEN` to provide the token through an environment variable.

Examples:

```powershell
# List microphone and line-input devices
python main.py --list-inputs

# Start with saved settings
python main.py --no-gui

# Start DRM mode with a playlist folder
python main.py --no-gui --mode DRM --audio-source playlist --playlist "D:\Radio\Playlist"
```
