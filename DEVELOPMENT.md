# Developer guide

## Validation

Run these commands from the repository root:

```bash
python -m compileall -q .
python -m unittest discover -s tests -v
```

Run `python main.py` to inspect the desktop app. Test browser-panel changes through the local Web GUI. Unit tests do not verify audio hardware, live TXITY sessions, or receiver decoding.

## Packaging and releases

`.github/workflows/release-builds.yml` builds a PyInstaller Windows x64 directory package and a Linux x86_64 AppImage. Commits to `main` replace the `rolling` prerelease. Running the workflow manually also creates the next patch version release tag, beginning at `v0.1.0`.

The workflow writes `build_info.py` before packaging. The updater reads that build number and checks the rolling release manifest in `multiv-update.json`. Assets are named with the workflow run number so the new manifest can be uploaded after the binaries without pointing clients at a partially replaced file. The manifest is the update channel's index and includes per-platform SHA-256 checksums.

The Windows package includes a separate console-mode `MultiVBrowserHelper.exe` beside `MultiV.exe`. `discord_auth.py` launches that helper in frozen builds so Python script arguments are never passed back to the PyInstaller application executable. Source runs continue to launch `discord_auth_browser.py` through the active Python interpreter.

The updater is for packaged desktop builds only. It verifies the downloaded package before scheduling installation after the running process exits. Windows launches the verified Inno Setup installer through UAC and restarts MultiV when installation finishes; Linux replaces the running AppImage file through a detached shell process. The user must approve the update.

## Product direction

MultiV is intended to support one desktop workflow across several virtual radio simulators. Keep audio-source selection separate from provider-specific connection code as backends are added.

See [ROADMAP.md](ROADMAP.md) for provider support and integration status. Keep this file focused on implementation details. Do not duplicate provider availability here.

## Main modules

- `main.py`: Tkinter desktop app and settings interface.
- `web_gui.py`: password-protected browser control panel.
- `transmitter.py`: TXITY transmission setup, audio buffering, and transport.
- `txity_client.py`: TXITY connection and account session.
- `drm_support.py` and `drm_encoder/`: DecDRM setup, station configuration, and encoder bridge.
- `nfm_modulator.py`: software NFM modulation and filtering.
- `freedv_rade.py` and `freedv_rade_tx.c`: FreeDV RADE V1 transmit encoder integration.
- `simtx_api.py`: SimTX account and protocol foundation. The desktop transmitter does not use this module.

Read [DESIGN.md](DESIGN.md) before changing an interface. Read [PRODUCT.md](PRODUCT.md) before changing product positioning. Keep the [README](README.md) as the user-facing entry page. Put detailed mode, configuration, and command-line guidance in the [reference pages](wiki/Home.md). Keep architecture and implementation notes here.

## SimTX implementation

`simtx_api.py` contains an initial implementation based on the documented SimTX account API and game protocol v25. It includes:

- Device-code authorization and access/refresh credential handling.
- Discovery of online servers and persistent stations.
- Single-use game-ticket requests bound to a station.
- TLS connection setup and the v25 protocol preface.
- Protobuf frame encoding and decoding.
- Initial audio-transmitter messages.

The desktop controls do not call this module. The active transmitter still connects to TXITY. MultiV does not yet have a complete SimTX transmitter, audio-capture pipeline, real-time sample pacing, continuous telemetry handling, or reconnect integration.

The account client keeps credentials in memory. Any caller that persists refresh credentials must use an operating-system credential store. Do not write refresh credentials to settings files or logs.

References: [SimTX API](https://wiki.simtx.net/index.php/SimTX_API) · [Device login](https://wiki.simtx.net/index.php/SimTX_API/Device_login) · [Transmitter session](https://wiki.simtx.net/index.php/SimTX_API/Transmitter_session).
