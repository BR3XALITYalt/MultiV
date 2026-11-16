# TXITPY

A Python desktop companion for the TXITY simulated radio-broadcast service, built with [Bootstack](https://bootstack.org/).

## Current scope

This first release provides a public viewer:

- Live public server status when the status WebSocket is available.
- A searchable station directory parsed from TXITY's public page.
- Quick links to the official TXITY site and this repository.

It does not send transmitter configuration, push-to-talk commands, or audio frames. It does not reuse browser session keys or login tokens. TXITY requires its official Discord sign-in for transmitter control and station editing; those authenticated features are not part of this viewer release.

## Requirements

- Python 3.12 or newer
- Tk/Tcl available on the system

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

The app fetches public data from `https://txity.radiodiscussion.org`. If the service changes its page markup or status protocol, station extraction or live status may stop working. The station directory still works independently of the status socket.
