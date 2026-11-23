# Built-in Icecast-compatible server

MultiV can host an HTTP MP3 stream itself, without an external Icecast provider.

## Configure

1. Open **Transmit** and find **Icecast outputs**.
2. Enable **Host a stream from MultiV**.
3. Set the TCP port and mountpoint.
4. Optionally set a listener password.
5. Set the maximum number of simultaneous listeners.
6. Start the MultiV transmission.

The server starts with the transmission and stops when the transmission stops. The stream uses the selected MP3 bitrate and is encoded once for both built-in listeners and external Icecast destinations.

## Connect

With the default port and mountpoint, the local stream URL is:

`http://127.0.0.1:8000/live.mp3`

Other devices on the same network can connect using the computer's LAN IP address, for example:

`http://192.168.1.20:8000/live.mp3`

The server binds to all network interfaces. To make it reachable outside the local network, the host network must allow inbound TCP connections and the router may need a port-forwarding rule. Do not expose a stream publicly unless you intend to share it.

If a listener password is configured, players must use HTTP Basic authentication with username `listener` and the configured password.

## Compatibility and limitations

This is an embedded Icecast-compatible HTTP MP3 listener endpoint, not the full upstream Icecast daemon. It supports one configured mountpoint, MP3 streaming, optional listener authentication, and a maximum listener count. It does not implement Icecast's full administration API, source protocol, relays, multiple mounts, or statistics endpoints.

The server runs only while the MultiV transmission is active. DRM and CW modes do not support the shared MP3 output path.
