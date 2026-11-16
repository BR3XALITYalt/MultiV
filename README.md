# TXITPY

A Python desktop companion for the TXITY simulated radio-broadcast service, built with [Bootstack](https://bootstack.org/).

## Current scope

This release provides a read-only desktop viewer with the official TXITY Discord sign-in flow:

- Live server status and a searchable public station directory.
- An embedded browser that loads TXITY itself and lets TXITY handle Discord OAuth, redirects, token validation, and sign-out.
- The authenticated status WebSocket receives the session token through TXITY's `authToken` query parameter.

The browser keeps the website's normal session in its own browser profile. TXITPY keeps the token in memory and does not write it to an application settings file or log it. The desktop client remains read-only: it sends status requests only, never transmitter configuration, push-to-talk commands, or audio frames.

## Requirements

- Python 3.12 or newer
- A supported WebView runtime for pywebview (Microsoft Edge WebView2 on Windows)

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

The app fetches public data from `https://txity.radiodiscussion.org`. If the service changes its page markup or status protocol, station extraction or live status may stop working. Discord sign-in depends on TXITY's current localStorage keys and official page flow; those are deliberately read by exact key and only while the embedded browser is on the TXITY HTTPS origin.
