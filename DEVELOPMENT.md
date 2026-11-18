# Developer notes

## Validation

Run these commands from the repository root:

```bash
python -m compileall -q .
python -m unittest discover -s tests -v
```

Run `python main.py` to inspect the desktop application. Test browser-control changes through the local Web GUI as well. Unit tests do not verify audio hardware, live TXITY sessions, or receiver decoding.

## Product direction

MultiV is intended to provide one desktop workflow for several virtual radio simulators. Keep audio-source selection separate from simulator-specific connection code as additional backends are developed.

See [ROADMAP.md](ROADMAP.md) for the current supported providers and integration status. Keep the implementation notes below focused on code behavior rather than duplicating provider-support claims. The active desktop transmitter and the SimTX foundation are described in the relevant module notes.

## Main modules

- `main.py`: Tkinter desktop application and settings UI.
- `web_gui.py`: password-protected browser control panel.
- `transmitter.py`: TXITY transmission setup, audio buffering, and transport.
- `txity_client.py`: TXITY connection and account session.
- `drm_support.py` and `drm_encoder/`: DecDRM setup, station configuration, and encoder bridge.
- `nfm_modulator.py`: software NFM modulation and filtering.
- `freedv_rade.py` and `freedv_rade_tx.c`: FreeDV RADE V1 transmit encoder integration.
- `simtx_api.py`: SimTX account and protocol foundation. It is not connected to the desktop transmitter.

Read [DESIGN.md](DESIGN.md) before changing either interface. Read [PRODUCT.md](PRODUCT.md) before changing product positioning. Keep user-facing guidance in [README.md](README.md); keep implementation notes here.

## SimTX implementation notes

`simtx_api.py` contains an initial implementation based on the documented SimTX account API and game protocol v25. It includes:

- Device-code authorization and access/refresh credential handling.
- Online server and persistent-station discovery.
- Single-use, station-bound game-ticket requests.
- TLS connection setup, the v25 protocol preface, protobuf frame encoding and decoding, and initial audio-transmitter messages.

The desktop controls do not use this module. The active transmitter still connects to TXITY. MultiV does not yet provide a complete SimTX transmitter, audio capture pipeline, real-time sample pacing, continuous telemetry handling, or reconnect integration.

The account client keeps credentials in memory. Any caller that persists refresh credentials must use an operating-system credential store. Do not write refresh credentials to settings files or logs.

References: [SimTX API](https://wiki.simtx.net/index.php/SimTX_API) · [Device login](https://wiki.simtx.net/index.php/SimTX_API/Device_login) · [Transmitter session](https://wiki.simtx.net/index.php/SimTX_API/Transmitter_session).
