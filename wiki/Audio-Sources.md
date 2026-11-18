# Audio sources and playlists

MultiV uses one audio source for each transmission. It does not combine the microphone, desktop audio, and playlist into a single mix.

## Microphone

Open **Transmit**, select **Microphone**, and choose an input device.

## Desktop audio

Select **Desktop audio** and choose a Windows playback device. Desktop-audio capture is not available in DRM mode.

## Playlists

Select **Playlist** and add audio files or a folder. MultiV supports:

- MP3
- WAV
- FLAC
- OGG
- Opus
- M4A
- AIFF

Use **Shuffle playlist** to randomize playback order and **Loop playlist** to repeat the list.

When files in an imported folder change, MultiV refreshes the folder's contents. Changes to the active playlist take effect the next time playback starts.

### DRM restrictions

DRM mode accepts microphone input and OGG, MP3, or FLAC playlists. It does not accept desktop-audio loopback. WAV and AIFF entries are skipped while DRM is selected.

