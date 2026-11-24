# MultiV interface design

## Product

MultiV is a multi-source, multi-stream client for virtual radio simulators. Users select microphone, desktop-audio, or playlist input, configure a station, choose a signal mode, and transmit through a simulator backend. See [ROADMAP.md](ROADMAP.md) for provider support and integration status.

## Design dials

- **Energy 2:** Use blue for the primary action and keyboard focus. Keep other surfaces quiet so users can scan controls and status values.
- **Rhythm 2:** Organize each page around its task. The Transmit page puts audio input and independently controlled TXITY and Icecast outputs first, followed by station identity and signal settings. The TXITY menu opens the searchable station directory in a separate window.
- **Motion 1:** Use native hover, focus, and pressed states. Do not add decorative transitions or looping animation.

## Desktop and browser interface

### Visual system

- **Theme:** Use ttkbootstrap for standard controls and its themed Canvas, Listbox, Text, Menu, and Toplevel classes. Keep the existing Sun Valley light/dark color values in MultiV's custom ttkbootstrap themes.
- **Dark theme:** Keep the layout and information hierarchy the same in both themes. Change surface and text colors, not page structure.
- **Typography:** Use Segoe UI. Use size and weight to establish hierarchy.
- **Panels:** Use rectangular panels to group related controls or station records. Keep unrelated content on the page surface.
- **Navigation:** Keep Transmit, Discord, and Settings in the signed-in top bar. Put TXITY website and station-directory actions in a TXITY dropdown menu.
- **Station records:** Display an image with its station metadata in the separate station-directory window. If no approved image URL is available, show the station name.
- **Output controls:** Show TXITY and Icecast as separate collapsible rows. Each row has an independent enable toggle and a drag handle for reordering. Drag a handle to move its row; Up and Down keys provide keyboard reordering. Preserve the selected order and enabled state.
- **Transmit page:** Keep Start transmission, Stop transmission, and the current status together in the page header. Place playlist actions beside the track list and its shuffle and loop controls.
- **Advanced settings:** Keep signal fading and audio-processing controls in the collapsed **Advanced settings** section. Keep station, signal, and audio-source controls visible.
- **Assets:** Use station images only when TXITY or Discord provides an approved URL. Do not invent station logos or profile images. Settings accepts PNG, JPEG, WebP, and GIF uploads.

## Public website

These rules apply to the site in `docs/`. They do not replace the desktop and browser-panel rules above.

- **Identity:** MultiV is a BR3XALITY virtual-radio client. Use the supplied wide banner as the wordmark lockup and the supplied square mark as the favicon.
- **Audience:** Users of virtual radio simulators who need one desktop client for audio input, transmission settings, and station management.
- **Mood:** Technical, restrained, and broadcast-oriented. Avoid generic SaaS launch-page patterns.
- **Palette:** Use a deep plum-charcoal page surface (`#15111A`), near-white text (`#F3EEF5`), muted text (`#BFB4C6`), and lavender accent (`#B58AE5`). The purple palette comes from the supplied artwork and brand direction. Reserve the accent for important links and the frequency-marker motif.
- **Typography:** Use the system sans-serif stack. It avoids a font download and keeps technical copy readable. Use size and weight to establish hierarchy.
- **Composition:** Use a split hero, audio-source list, station definition list, signal-mode rows, provider-status link, and two-column installation section. Each structure follows the content in that section.
- **Identity motif:** Use a small frequency marker with uneven ticks beside section labels. It references radio tuning without inventing frequencies or displaying decorative values as live data.
- **Imagery:** Show the supplied banner as one image so its blended wordmark stays intact. Use the supplied Unsplash image only in the bounded hero area. Keep text off that image so contrast does not depend on its pixels. Do not create additional artwork or screenshots.
- **Surfaces:** Use solid page and section backgrounds with thin separators. Do not add blur, glow, or broad shadows without a clear interface purpose.
- **Motion:** Keep the page static. Provide hover, active, and focus states for links. Honor reduced-motion preferences. Do not add scroll reveals, parallax, or animated meters.
- **Theme:** Keep the public site in its dark brand theme. The supplied wordmark and purple palette are designed for a dark field.
- **Dials:** ENERGY 2 / RHYTHM 2 / MOTION 1.

## Interface copy

- Use the same name for each control in the desktop app, browser panel, and README.
- Use short, specific labels in sentence case. Name the action on buttons.
- Report only the state the app has reached. Name the failed operation in error messages and give the next useful action.
- Put units, supported formats, and important limits beside the relevant setting.
- Remove vague promises, filler, and decorative slogans.

## Accessibility

- Use native buttons, entries, checkboxes, scales, and comboboxes.
- Make every action available by keyboard and keep focus visible.
- Pair color feedback with text. Check contrast in both themes.
- Keep labels visible beside or above controls.
- Give images useful alternative text. Expose browser-panel status messages to assistive technology.
