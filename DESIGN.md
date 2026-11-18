# MultiV interface design

## Product

MultiV is a multi-source, multi-stream client for virtual radio simulators. Users select microphone, desktop-audio, or playlist input, configure a virtual station, choose a signal mode, and transmit through a simulator backend. See [ROADMAP.md](ROADMAP.md) for supported providers and integration status.

## Design dials

- **Energy 2:** Blue marks the primary action and keyboard focus. Keep other surfaces quiet so controls and status values remain easy to scan.
- **Rhythm 2:** Organize each page around a task. Transmit groups station identity, signal settings, audio processing, input, CW, and connection state. Stations presents station records with their images and metadata.
- **Motion 1:** Use native hover, focus, and pressed states. Scrolling follows the user's input. Do not add decorative easing or looping animation.

## Visual system

- **Palette:** Use Material surface roles for page and panel backgrounds. Use blue `#185ABC` for primary actions, links, and focus. Use cyan `#00677E` for secondary signal information. Reserve red for errors.
- **Dark theme:** Keep the same layout in light and dark themes. Change surface and text roles, not the information hierarchy.
- **Typography:** Use Segoe UI. Establish hierarchy with size and weight, not decorative display fonts.
- **Panels:** Use rectangular panels to group related controls or station records. Keep unrelated content on the page surface.
- **Navigation:** Keep the four signed-in destinations in a horizontal top bar.
- **Station records:** Keep each station's image and metadata together. Show the station name when no approved image URL is available.
- **Transmit page:** Keep Start/Stop and its current status together in the page header. Place playlist actions beside the track list and its shuffle and loop controls.
- **Advanced settings:** Keep signal fading and audio-processing controls inside the collapsed **Advanced settings** section. Do not hide station, signal, or audio-source controls there.
- **Assets:** Display station images only when TXITY or Discord provides an approved URL. Do not invent station logos or profile images. Settings supports PNG, JPEG, WebP, and GIF uploads.

## Interface copy

- Use the same name for a control in the desktop app, browser panel, and README.
- Use short, specific labels. Button text names the action.
- Write status text for the state the app has reached. Error text names the failed operation and the next useful action.
- Put units, supported formats, and important limits beside the relevant setting.
- Avoid marketing language, vague promises, decorative slogans, and explanations of internal implementation.

## Accessibility

- Use native buttons, entries, checkboxes, scales, and comboboxes.
- Make every action available by keyboard and keep focus visible.
- Pair color feedback with text. Check contrast in light and dark themes.
- Keep labels visible beside or above their controls.
- Give images appropriate text alternatives. Keep status messages available to assistive technology in the browser panel.

Record the reason in this file when a major visual decision changes.
