# TXITPY

A Python desktop companion for the TXITY simulated radio-broadcast service, built with the standard-library Tkinter and ttk GUI toolkits.

## Current scope

This release provides a read-only desktop viewer with the official TXITY Discord sign-in flow:

- Live server status and a searchable public station directory.
- TXITY's official Discord OAuth flow in an embedded browser window, followed by a one-use loopback callback into the desktop app.
- The authenticated status WebSocket receives the session token through TXITY's `authToken` query parameter.

Discord sign-in is required before the dashboard can be used. On startup, TXITPY checks the existing app-specific WebView2 profile in a hidden browser window. If TXITY still has a valid saved session, the dashboard unlocks automatically; otherwise the sign-in page remains open. The browser profile keeps the website's normal session across launches. TXITPY keeps the active token in memory and does not write it to an application settings file or log it. Disconnect clears the saved browser profile. The only authenticated control message sent by the desktop client is TXITY's `discordLogout` notification when the user disconnects. It never sends transmitter configuration, push-to-talk commands, or audio frames.

The desktop interface uses native Tkinter and ttk widgets with a blue Material 3 Expressive-inspired scheme based on TeleDrive's light and dark palettes: blue primary (`#1B6EF3`), cyan tertiary (`#00677E`), and layered Material-style surfaces. A light/dark switch follows the Windows application theme on startup and can be changed from the toolbar. Color values are adapted from [TeleDrive](https://github.com/Mahmud0808/TeleDrive), which uses Kotlin Multiplatform and Compose Material 3 Expressive.

## Requirements

- Python 3.12 or newer
- Microsoft Edge WebView2 Runtime on Windows

Install the dependencies:

```bash
python -m pip install -r requirements.txt
```

Run the app:

```bash
python main.py
```

## Tests

```bash
python -m unittest discover -s tests -v
```

## Notes

The app fetches public server configuration and the station directory from `https://txity.radiodiscussion.org`, using `/api/config` and `/users.json`. The JSON listing supplies live client counts and station metadata; comment history and page-layout data are ignored. Station refresh results and fetch errors are shown directly on the Stations page. If the service changes the JSON schema or status protocol, station extraction or live status may stop working. Discord sign-in is enabled only when TXITY reports `secureDiscordAuth: true`. TXITY currently returns the signed session to its own homepage, so the embedded browser waits for TXITY's frontend to store the session in same-origin browser storage. It reads that value only on `https://txity.radiodiscussion.org` and forwards it through the app's temporary loopback callback. The callback is bound to loopback only, protected by a single-use random state value, expires after five minutes, and shuts down after use.
