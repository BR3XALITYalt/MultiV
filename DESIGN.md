# MultiV interface direction

## Product

MultiV is a Windows desktop client for TXITY's simulated radio-broadcast service. The main tasks are checking TXITY service status, finding stations, managing a Discord session, and configuring a virtual audio stream.

## Design read

Reading this as: a desktop operations client for TXITY users, using native Tkinter controls and a Material 3-inspired visual language, with ENERGY 2 / RHYTHM 2 / MOTION 1.

- **Energy 2:** the blue primary color marks the main action and keyboard focus. The rest of the interface stays quiet enough for status values and configuration fields to lead.
- **Rhythm 2:** each page follows its task. Transmit is the opening screen and groups station identity, signal settings, audio processing, input, CW, and connection state. Stations uses image-led station records.
- **Motion 1:** use native hover, focus, and pressed feedback. Scrolling follows the user's input without decorative easing or looping animation.

## Visual decisions

- **Palette:** use Material surface roles for page and panel backgrounds. Use blue #185ABC for primary actions, links, and focus because it meets normal-text contrast on the light surfaces. Use cyan #00677E for secondary signal information, and reserve red for errors.
- **Dark theme:** retain the paired Material dark palette so the light/dark switch changes surfaces and text roles without changing the interface structure.
- **Typography:** use Segoe UI because it is available on Windows and reads like a native desktop utility. Use size and weight for hierarchy rather than decorative display fonts.
- **Surfaces:** use rectangular Material-style panels to group real station records and related controls. Keep most content on the page surface; a panel should indicate a useful grouping, not fill empty space.
- **Navigation:** keep the four existing destinations in a horizontal top bar because they are the app's actual pages and remain available while signed in.
- **Station list:** keep image and metadata together in each station record so users can scan identity, frequency, mode, and operator without opening every profile.
- **Transmit page:** group controls by the settings they affect. Keep the transmission action and its current status together so the operator can check the result of a start or stop. The audio source section offers microphone, desktop loopback, and playlist modes; playlist actions stay beside the track list and its shuffle and loop checkboxes.
- **Simplified transmit view:** keep the primary Start/Stop action in the page header so it stays reachable without scrolling. Hide signal fading and audio-processing controls under a collapsed **Advanced settings** section; those controls remain available without competing with station, signal, and audio-source setup.
- **Spacing:** use compact spacing inside related fields and larger gaps between sections. This keeps the settings page dense without merging unrelated controls.
- **Assets:** display station images only when TXITY or Discord supplies an approved URL. Use the station name as text when no image is available; do not invent station logos or profile images. The Settings page lets the user upload a PNG, JPEG, WebP, or GIF logo and keeps the returned TXITY URL beside the manual URL field.

## Accessibility

- Use native buttons, entries, checkboxes, scales, and comboboxes.
- Keep every action reachable by keyboard, with a visible focus indicator.
- Pair color feedback with text. Use the same contrast checks in light and dark themes.
- Keep labels visible beside or above their controls.
