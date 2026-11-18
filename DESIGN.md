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

## Website direction

This section applies to the public site in `docs/`. It does not replace the desktop and browser-panel rules above.

- **Identity:** MultiV is a BR3XALITY virtual-radio client. The supplied wide banner is the wordmark lockup; the supplied square mark is used as the favicon.
- **Audience:** People who use virtual radio simulators and need one desktop client for audio input, transmission settings, and station management.
- **Mood:** Restrained, technical, and broadcast-oriented. The page should feel like a piece of radio software, not a generic SaaS launch page.
- **Palette:** Use a deep plum-charcoal page surface (`#15111A`), warm near-white text (`#F3EEF5`), muted neutral text (`#BFB4C6`), and one lavender accent (`#B58AE5`). The purple direction comes from the owner's explicit brand request and supplied artwork. Keep the accent on important links and the frequency-marker motif rather than spreading it across every element.
- **Typography:** Use the browser's system sans-serif stack. It loads without a font request and keeps dense technical copy readable. Use weight, scale, and line length for hierarchy.
- **Composition:** Use a split hero, a source list, a station-capability definition list, signal-mode rows, a plain provider-status link, and a two-column installation section. The content structures vary by task while the two-column frame keeps the page easy to scan. Do not repeat a feature-card grid.
- **Identity motif:** A small frequency-marker rule with uneven ticks appears beside section labels. It references radio tuning without inventing frequencies or presenting decorative values as live data.
- **Imagery:** Display the supplied banner as one complete image so its blended wordmark is not recreated with HTML text. Use the owner-provided Unsplash gradient image only as a bounded hero image. Keep text off the image so contrast does not depend on its changing pixels. Do not generate additional art or screenshots.
- **Surfaces:** Use solid page and section surfaces with thin separators. Avoid full-page blur, stacked glass panels, glow, and broad shadows. These effects add decoration without helping users understand the product.
- **Motion:** Keep the page static. Links receive clear hover, active, and focus states; honor reduced-motion preferences. Do not add scroll reveals, parallax, or animated meters.
- **Theme:** Keep the public site in its dark brand theme. The supplied wordmark and requested purple direction use a dark field; this is a brand decision, not an arbitrary technology aesthetic.
- **Dials:** ENERGY 2 / RHYTHM 2 / MOTION 1.

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
