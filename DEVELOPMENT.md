# Developer guide

## Validation

Run these commands from the repository root:

```bash
python -m compileall -q .
python -m unittest discover -s tests -v
```

Run `python main.py` to inspect the desktop app. Test browser-panel changes through the local Web GUI. Unit tests do not verify audio hardware, live TXITY sessions, or receiver decoding.

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

Read [DESIGN.md](DESIGN.md) before changing an interface. Read [PRODUCT.md](PRODUCT.md) before changing product positioning. Put user instructions in [README.md](README.md) and implementation notes here.

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
