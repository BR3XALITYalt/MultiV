# Icecast outputs

MultiV can send the selected audio source to up to eight Icecast servers while it transmits through TXITY.

## Configure a destination

1. Open **Transmit**.
2. Find **Icecast outputs**.
3. Select **Add server**.
4. Enter the server URL, including its mountpoint. For example, \`https://radio.example:8443/live.mp3\`.
5. Enter the source username and password supplied by the server administrator.
6. Leave **Enable this destination** selected and save.

The URL must use HTTP or HTTPS and must not contain embedded credentials, query parameters, or a fragment. The default port is 8000 for HTTP and 443 for HTTPS. A non-default port can be included in the URL.

MultiV supports up to eight destinations. Select a destination and choose **Edit** or **Remove** to manage it. Disabled destinations remain saved but are not connected.

## Audio format

- Codec: MP3 using libmp3lame
- Sample rate: 48 kHz
- Channels: mono
- Bitrates: 64, 96, 128, 160, 192, 256, or 320 kbps

The bitrate applies to all enabled destinations. MultiV encodes the audio once and sends the encoded stream to each destination.

## Transmission behavior

Icecast output uses the same selected microphone, desktop-audio, or playlist input as the TXITY transmission. It starts when the transmission starts and stops when the transmission stops. Each destination has its own connection and reconnect loop, so one unavailable server does not prevent the others from receiving audio.

Icecast output is not available in DRM or CW mode. In other modes, the Icecast stream carries the selected source audio, not the simulator's modulated RF signal.

A connection status is shown below the destination list. Authentication failures, rejected mountpoints, and connection errors are reported per destination.

Source passwords are stored with the local MultiV settings. Use a dedicated source account and a unique password for each server.
