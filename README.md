![MultiV](assets/banner.svg)

**A multi-source, multi-stream client for virtual radio simulators.**

MultiV is a desktop app for setting up a station and transmitting audio through a supported simulator. Choose one audio source for each transmission.

## Install

Install Python 3.12 or newer. On Windows, install the Microsoft Edge WebView2 Runtime.

From the project folder, run:

```powershell
python -m pip install -r requirements.txt
python main.py
```

## Start a transmission

1. Sign in with Discord.
2. Open **Transmit**.
3. Enter your station name, frequency, and signal type.
4. Choose an audio source.
5. Select **Start transmission**.

MultiV uses one audio source at a time. It does not mix the microphone, desktop audio, and playlist together.

## Audio sources

- **Microphone**
- **Desktop audio** on Windows
- **Playlist** from audio files or a folder

See the [audio and playlist guide](wiki/Audio-Sources.md) for supported formats and playlist behavior.

## Signal modes

MultiV includes AM, sideband, NFM, FreeDV RADE V1, DRM (experimental), and CW. RADE V1 is transmit-only. Signal behavior and compatibility depend on the selected mode and simulator.

See the [signal mode reference](wiki/Signal-Modes.md) before using NFM, RADE, or DRM.

## Station controls

Use **Settings** to edit your station profile and logo. Use **Stations** to browse the TXITY station directory.

MultiV also includes a browser control panel for station settings and transmission controls. The panel uses HTTP and is intended for a trusted local network only. Do not expose it to the public internet. See the [browser control panel guide](wiki/Browser-Control-Panel.md).

## Documentation

- [Reference guide](wiki/Home.md)
- [Audio sources and playlists](wiki/Audio-Sources.md)
- [Signal modes](wiki/Signal-Modes.md)
- [Browser control panel](wiki/Browser-Control-Panel.md)
- [Configuration and command line](wiki/Configuration-and-CLI.md)
- [Provider support](ROADMAP.md)

