# Built-in Icecast-compatible server

MultiV can host an HTTP audio stream directly. The built-in listener uses the same format-specific FFmpeg/PyAV encoder pipeline as remote Icecast destinations.

## Configure

1. Open **Transmit** and expand **Icecast**.
2. Enable the Icecast provider and **Host a stream from MultiV**.
3. Choose an audio format and bitrate.
4. Set the TCP port and mountpoint.
5. Optionally set a listener password.
6. Set the maximum number of simultaneous listeners.
7. Start transmission.

The server starts with the transmission and stops when it ends. The selected format is sent with its matching HTTP content type. Available formats depend on the encoders in the installed FFmpeg/PyAV build:

- **Ogg Opus**: recommended default when available. Minimum 48 kbps.
- **Ogg Vorbis**: bitrate selected from the available bitrate list.
- **AAC+ (HE-AAC)**: requires the libfdk_aac encoder and supports up to 128 kbps.
- **Ogg FLAC**: lossless. No bitrate setting is used.
- **MP3**: available as a compatibility option.

MultiV selects the first available format in this preference order: Ogg Opus, Ogg Vorbis, AAC+, Ogg FLAC, then MP3. The list in the application reflects the encoders actually available in its FFmpeg/PyAV build. If a format is absent, its encoder is unavailable.

## Connect

With the default port and mountpoint, the local stream URL is:

`http://127.0.0.1:8000/live`

Other devices on the same network can connect using the computer's LAN IP address, for example:

`http://192.168.1.20:8000/live`

The mountpoint does not need a file extension. The server sends the selected format's content type. The server binds to all network interfaces. To make it reachable outside the local network, the host network must allow inbound TCP connections and the router may need a port-forwarding rule. Do not expose a stream publicly unless you intend to share it.

If a listener password is configured, players must use HTTP Basic authentication with username `listener` and the configured password.

## Compatibility and limitations

This is an embedded Icecast-compatible HTTP listener endpoint, not the full upstream Icecast daemon. It supports one configured mountpoint, one selected audio format, optional listener authentication, and a maximum listener count. It does not implement Icecast's full administration API, source protocol, relays, multiple mounts, or statistics endpoints.

The server runs while MultiV's audio transmission is active. DRM and CW modes do not support the shared Icecast audio path.
