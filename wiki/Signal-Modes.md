# Signal modes

Mode availability depends on the selected simulator and its transport. The details below describe the current TXITY path.

## NFM

MultiV generates the FM signal in software and sends it through TXITY's USB or LSB audio transport. TXITY does not provide native NFM.

| Profile | Occupied bandwidth | IF | Peak deviation | Voice band |
| --- | ---: | ---: | ---: | ---: |
| Standard | 9 kHz | 5 kHz | 2.5 kHz | 30-2,000 Hz |
| Wide | 11 kHz | 6 kHz | 2.5 kHz | 100-3,000 Hz |

Each profile leaves 500 Hz between the IF and the sum of peak deviation and the upper voice-band limit.

The **NFM transport bandwidth** setting requests a TXITY passband from 1 to 20 kHz. The default is 12 kHz. A narrower passband can clip the generated signal.

NFM works only in TXITY's 7.000-8.000 MHz room. An independent receiver test has not verified this signal path. TXITY filtering and receiver settings can affect the result. The selected IF shifts the audio transport so the entered frequency remains the channel centre.

## FreeDV RADE V1

RADE V1 uses the RADE C modem and LPCNet/FARGAN speech encoder.

MultiV converts the modem output to a real 8 kHz audio waveform, resamples it to 48 kHz, and sends it through TXITY's USB audio transport. The entered frequency is the USB suppressed-carrier reference. The RADE audio centre is 1.5 kHz, and the transport passband is 3 kHz.

The first time RADE starts, MultiV downloads a Windows x64 encoder package from a MultiV release and verifies its SHA-256 checksum before installation. Encoding uses significant CPU time. MultiV buffers audio before keying and stops with an error if the encoder cannot keep up.

TXITY does not provide a RADE decoder or receive-audio stream to MultiV. RADE mode is transmit-only.

References: [FreeDV RADE](https://freedv.org/radio-autoencoder/) and [RADE C](https://github.com/freedv/rade_c). The runtime package includes BSD and Opus notices.

## DRM (experimental)

DRM has three profiles:

| Profile | Occupancy | MSC modulation | Audio |
| --- | ---: | --- | --- |
| Robust | 4.5 kHz | 16-QAM | Mono |
| Quality | 9 kHz | 16-QAM | Mono |
| Maximum quality | 9 kHz | 64-QAM | Stereo |

The SDC uses 16-QAM in all profiles. 64-QAM and stereo carry more data but need better reception. Maximum quality can perform worse on a weak or fading channel.

Long MSC interleaving adds about 2 seconds of delay and improves resistance to fading. Short interleaving uses about 400 ms and reduces delay.

Choose xHE-AAC or HE-AAC. Use HE-AAC if the receiver does not support xHE-AAC. The encoder selects a bitrate that fits the available service capacity.

DRM accepts microphone input and OGG, MP3, or FLAC playlists. It does not support desktop-audio loopback. WAV and AIFF playlist entries are skipped in DRM mode.

### Encoder setup

When DRM is selected, MultiV checks for the encoder and asks before installing missing dependencies.

On Windows, setup can install the official Rust toolchain, download DecDRM 0.7.1 and its codec dependencies, and build the encoder. MultiV checks the Rust installer checksum before running it. The build requires Git, CMake, and Microsoft C++ Build Tools with the **Desktop development with C++** workload. Setup may request administrator approval.

On other platforms, install Rust and a working C/C++ compiler.

TXITY does not provide native DRM. MultiV sends the generated waveform through TXITY's DSB audio transport. An independent DRM receiver test has not verified this path.

See the [third-party notices](../drm_encoder/NOTICE.md) for the encoder and codec license details.

## CW

Select **CW** as the signal type. Enter the text in **CW message**, set the speed, and select **Repeat CW message** to send it continuously.

