# Configuration and command line

## Discord session

MultiV saves your Discord session between launches. Select **Disconnect Discord** to clear the saved browser session and token.

The Discord session token is stored separately from station and transmission preferences.

## Settings files

MultiV stores station and transmission preferences in these locations:

| Platform | File |
| --- | --- |
| Windows | `%APPDATA%\TXITPY\transmit-settings.json` |
| Linux and macOS | `$XDG_CONFIG_HOME/TXITPY/transmit-settings.json`, or `~/.config/TXITPY/transmit-settings.json` |

On Windows, the Discord session token is stored at `%PROGRAMDATA%\TXITPY\token.txt`. On Linux and macOS, MultiV uses the application configuration directory.

## Command line

Run this from the project folder to list the available options:

```bash
python main.py --help
```

Use `--no-gui` or `--nogui` to start a transmission from saved settings without opening the desktop window.

These options override saved settings:

- `--station-name`
- `--frequency`
- `--mode`
- `--power`
- `--audio-source`
- `--audio-device`
- `--playlist`

The headless client reads the saved token on startup. Use `--auth-token-file` to select another token file, or set `TXITPY_DISCORD_AUTH_TOKEN` to provide the token through an environment variable.

### Examples

List microphone and line-input devices:

```bash
python main.py --list-inputs
```

Start with saved settings:

```bash
python main.py --no-gui
```

Start DRM mode with a playlist folder:

```powershell
python main.py --no-gui --mode DRM --audio-source playlist --playlist "D:\Radio\Playlist"
```

## Linux desktop audio

List PulseAudio and PipeWire monitor sources:

```bash
python main.py --list-loopback-devices
```

Choose one of the listed device IDs with `--audio-device` when running headlessly. The selected monitor must be exposed as an input device by PortAudio.

## Linux systemd user service

MultiV handles SIGTERM and can run in a systemd user service. Create `~/.config/systemd/user/multiv.service` and replace the paths with the locations on your computer:

```ini
[Unit]
Description=MultiV transmitter
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/path/to/MultiV
ExecStart=/path/to/MultiV/.venv/bin/python /path/to/MultiV/main.py --no-gui
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

Load and start the service:

```bash
systemctl --user daemon-reload
systemctl --user enable --now multiv.service
```

View its output with `journalctl --user -u multiv.service -f`. The service runs as your user and reads the same token and settings files as the desktop application.

