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

```powershell
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

```powershell
python main.py --list-inputs
```

Start with saved settings:

```powershell
python main.py --no-gui
```

Start DRM mode with a playlist folder:

```powershell
python main.py --no-gui --mode DRM --audio-source playlist --playlist "D:\Radio\Playlist"
```

