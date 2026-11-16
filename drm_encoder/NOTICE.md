# DRM third-party notices

TXITPY's DRM30 bridge uses **DecDRM 0.7.1** by CasualArclamp, pinned to commit `05845c9a754309b86d4985b25b1c3a787b988104`.

DecDRM and the TXITPY Rust bridge are licensed under **GNU GPL version 2 or later**. The upstream source is cloned at first use, and its output-validation check is locally patched so TXITPY can read the frames returned by `Station::transmit_frame()` without writing a duplicate signal file. The full GPL version 2 text is provided in `COPYING`.

DecDRM includes codec submodules with their own licensing and notice files:

- FDK-AAC has separate licensing terms. Review its upstream `NOTICE` and license before redistributing a compiled helper.
- libxaac is separately licensed under Apache-2.0.
- Opus is separately licensed under its upstream license.

The build links the selected HE-AAC encoder and may compile additional codec components. The GPL license of DecDRM does not replace the independent terms of those components. Do not redistribute the compiled helper until every bundled codec license has been reviewed.

Upstream source: https://github.com/CasualArclamp/DecDRM

