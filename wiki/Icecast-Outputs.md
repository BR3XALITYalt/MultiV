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

## Audio format per destination

Open a destination's **Edit** dialog and choose its audio format and bitrate. The format is saved independently for each server.

MultiV follows the codec choices documented by BUTT (Broadcast Using This Tool):

- **MP3** - MPEG Layer III, sent as `audio/mpeg`
- **AAC+ (HE-AAC)** - available only when the installed FFmpeg build includes the FDK-AAC encoder; sent as `audio/aacp`
- **Ogg Vorbis** - Ogg container, sent as `application/ogg`
- **Ogg Opus** - Ogg container, sent as `application/ogg`
- **Ogg FLAC** - lossless FLAC in an Ogg container; bitrate is not applicable

MultiV checks which encoders are available and only offers those formats in the destination editor. MP3, AAC+, Vorbis, and Opus expose a per-destination bitrate selector. AAC+ is limited to 128 kbps or below; Ogg Opus starts at 48 kbps. Ogg FLAC is lossless.

The audio source is 48 kHz mono. Destinations with the same codec and bitrate share one encoder; destinations with different settings receive separately encoded streams. The built-in server remains MP3 and uses the separate **Built-in MP3 bitrate** setting.

## Transmission behavior

Icecast output uses the same selected microphone, desktop-audio, or playlist input as the TXITY transmission. It starts when the transmission starts and stops when the transmission stops. Each destination has its own connection and reconnect loop, so one unavailable server does not prevent the others from receiving audio.

Icecast output is not available in DRM or CW mode. In other modes, the Icecast stream carries the selected source audio, not the simulator's modulated RF signal.

A connection status is shown below the destination list. Authentication failures, rejected mountpoints, and connection errors are reported per destination.

Source passwords are stored with the local MultiV settings. Use a dedicated source account and a unique password for each server.
