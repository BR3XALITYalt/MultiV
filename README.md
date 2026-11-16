# TXITPY

A Python desktop companion for the TXITY simulated radio-broadcast service, built with [Bootstack](https://bootstack.org/).

## Current scope

This release provides a read-only desktop viewer with the official TXITY Discord sign-in flow:

- Live server status and a searchable public station directory.
- TXITY's official Discord OAuth flow in an embedded browser window, followed by a one-use loopback callback into the desktop app.
- The authenticated status WebSocket receives the session token through TXITY's `authToken` query parameter.

The embedded browser uses an app-specific WebView2 profile to keep TXITY's normal website session. TXITPY keeps the active token in memory and does not write it to an application settings file or log it. Disconnect clears the embedded browser profile. The only authenticated control message sent by the desktop client is TXITY's `discordLogout` notification when the user disconnects. It never sends transmitter configuration, push-to-talk commands, or audio frames.

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

The app fetches public server configuration and the station directory from `https://txity.radiodiscussion.org`, using `/api/config` and `/users.json`. The JSON listing supplies live client counts and station metadata; comment history and page-layout data are ignored. If the service changes the JSON schema or status protocol, station extraction or live status may stop working. Discord sign-in is enabled only when TXITY reports `secureDiscordAuth: true`. TXITY currently returns the signed session to its own homepage, so the embedded browser waits for TXITY's frontend to store the session in same-origin browser storage. It reads that value only on `https://txity.radiodiscussion.org` and forwards it through the app's temporary loopback callback. The callback is bound to loopback only, protected by a single-use random state value, expires after five minutes, and shuts down after use.
