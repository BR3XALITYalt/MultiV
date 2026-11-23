![MultiV](assets/banner.svg)

**A multi-source, multi-stream client for virtual radio simulators.**

MultiV is a desktop app for setting up a station and transmitting audio through a supported simulator. Choose one audio source for each transmission.

## Install

MultiV requires Python 3.12 or newer.

### Windows

Install the Microsoft Edge WebView2 Runtime, then run these commands from the project folder:

```powershell
python -m pip install -r requirements.txt
python main.py
```

### Linux

On Ubuntu or Debian, install the desktop, audio, and build dependencies:

```bash
sudo apt update
sudo apt install python3-tk python3-venv portaudio19-dev libportaudio2 build-essential cmake git pkg-config
```

Create a virtual environment using Python 3.12 or newer, then install MultiV:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python main.py
```

The Linux requirements install the Qt WebView backend used for Discord sign-in. Your desktop session must provide a working Qt display environment.

MultiV uses PortAudio for microphone input. Desktop-audio capture uses a PulseAudio or PipeWire monitor source exposed as an input device. Select **Desktop audio (output loopback)** and choose the matching monitor source.

The prebuilt RADE encoder is available for Windows x64 and Linux x86_64. DRM builds its encoder locally on Linux and can install the Rust toolchain automatically. DRM also needs Git, CMake, and a C/C++ compiler. Headless operation can run under a Linux systemd user service; see the [command-line guide](wiki/Configuration-and-CLI.md).

## Packaged builds

Download the [latest rolling build](https://github.com/BR3XALITYalt/MultiV/releases/tag/rolling) or choose a numbered release from the [releases page](https://github.com/BR3XALITYalt/MultiV/releases).

- **Windows:** Run the installer. It defaults to `C:\Program Files\MultiV`; the desktop shortcut is optional during setup.
- **Linux:** Run `chmod +x MultiV-linux-x86_64-*.AppImage`, then open the AppImage file.

Packaged builds check for updates when they start. MultiV asks before installing an update and restarting. Windows updates launch the installer with an elevation prompt; Linux updates replace the AppImage in its current folder.

## Start a transmission

1. Sign in with Discord.
2. Open **Transmit**.
3. Enter your station name, frequency, and signal type.
4. Choose an audio source.
5. Select **Start transmission**.

MultiV uses one audio source at a time. It does not mix the microphone, desktop audio, and playlist together.

## Audio sources

- **Microphone**
- **Desktop audio** through Windows WASAPI or Linux PulseAudio/PipeWire monitor sources
- **Playlist** from audio files or a folder

See the [audio and playlist guide](wiki/Audio-Sources.md) for supported formats and playlist behavior.

## Icecast outputs

Add up to eight Icecast destinations on the **Transmit** page. Each destination uses its server URL and mountpoint, source username, and source password. MultiV sends mono MP3 at 48 kHz to every enabled destination. Select 64, 96, 128, 160, 192, 256, or 320 kbps.

Icecast output runs alongside the TXITY transmission and uses the selected microphone, desktop-audio, or playlist source. It is unavailable in DRM and CW modes. Each server reconnects independently if its connection drops. See the [Icecast output guide](wiki/Icecast-Outputs.md) for configuration and connection details.

## Signal modes

MultiV includes AM, sideband, NFM, FreeDV RADE V1, DRM (experimental), and CW. RADE V1 is transmit-only. Signal behavior and compatibility depend on the selected mode and simulator.

See the [signal mode reference](wiki/Signal-Modes.md) before using NFM, RADE, or DRM.

## Station controls

Use **Settings** to edit your station profile and logo. Use **Stations** to browse the TXITY station directory.

MultiV also includes a browser control panel for station settings and transmission controls. The panel uses HTTP and is intended for a trusted local network only. Do not expose it to the public internet. See the [browser control panel guide](wiki/Browser-Control-Panel.md).

## Documentation

- [Reference guide](wiki/Home.md)
- [Audio sources and playlists](wiki/Audio-Sources.md)
- [Icecast outputs](wiki/Icecast-Outputs.md)
- [Signal modes](wiki/Signal-Modes.md)
- [Browser control panel](wiki/Browser-Control-Panel.md)
- [Configuration and command line](wiki/Configuration-and-CLI.md)
- [Provider support](ROADMAP.md)

