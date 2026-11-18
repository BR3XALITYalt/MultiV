# Browser control panel

The browser control panel can edit station settings and start or stop a transmission. Use the desktop app to select audio devices and playlist files.

## Enable the panel

1. Open **Settings** in MultiV.
2. Set a password with at least 12 characters.
3. Enable **Web GUI**.
4. Keep MultiV running while using the panel.

MultiV listens on port `23234` on all network interfaces.

Open the panel on the same computer:

```text
http://127.0.0.1:23234
```

To connect from another device on the same network, open the computer's LAN IP address with port `23234`.

## Network security

The panel uses HTTP, not HTTPS. Use it only on a trusted local network.

Do not expose port `23234` to the public internet. Do not forward the port through your router or place the panel behind a public reverse proxy.

MultiV stores a salted PBKDF2 password hash. Authenticated browser sessions are held in memory.

