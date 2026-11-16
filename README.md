# TXITPY

A Python desktop companion for the TXITY simulated radio-broadcast service, built with [Bootstack](https://bootstack.org/).

## Current scope

This release provides a read-only desktop viewer with the official TXITY Discord sign-in flow:

- Live server status and a searchable public station directory.
- TXITY's official Discord OAuth flow opened in the system browser, followed by a one-use loopback callback into the desktop app.
- The authenticated status WebSocket receives the session token through TXITY's `authToken` query parameter.

The system browser keeps the website's normal session in its own browser profile. TXITPY keeps the token in memory and does not write it to an application settings file or log it. The only authenticated control message sent by the desktop client is TXITY's `discordLogout` notification when the user disconnects. It never sends transmitter configuration, push-to-talk commands, or audio frames.

## Requirements

- Python 3.12 or newer
- A system web browser capable of opening TXITY's Discord sign-in page

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

The app fetches public data from `https://txity.radiodiscussion.org`. If the service changes its page markup or status protocol, station extraction or live status may stop working. Discord sign-in is enabled only when TXITY reports `secureDiscordAuth: true`. It also depends on TXITY allowing the `return` parameter to point to the app's temporary `127.0.0.1` callback. That callback is bound to loopback only, protected by a single-use random state value, expires after five minutes, and shuts down after use. The supplied frontend code establishes the token fragment format, but it does not establish the server's redirect allowlist; that backend behavior still needs live verification.
