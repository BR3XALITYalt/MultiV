# Third-party notices

MultiV's DRM30 bridge uses **DecDRM 0.7.1** by CasualArclamp, pinned to commit `05845c9a754309b86d4985b25b1c3a787b988104`.

DecDRM and the TXITY Rust bridge are licensed under **GNU GPL version 2 or later**. MultiV clones the upstream source on first use. The local output-validation patch lets TXITY read frames returned by `Station::transmit_frame()` without writing a duplicate signal file. The full GPL version 2 text is in `COPYING`.

DecDRM includes codec submodules with separate license terms and notice files:

- **FDK-AAC:** Has separate licensing terms. Review its upstream `NOTICE` and license before redistributing a compiled helper.
- **libxaac:** Licensed separately under Apache-2.0.
- **Opus:** Distributed under its upstream license.

The build links the selected HE-AAC encoder and may compile other codec components. DecDRM's GPL license does not replace the separate terms for those components. Review every bundled codec license before redistributing the compiled helper.

Upstream source: https://github.com/CasualArclamp/DecDRM
