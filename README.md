<p align="center">
  <img src="assets/icons/hypercycle.svg" alt="Hypercycle" width="120">
</p>

<h1 align="center">Hypercycle</h1>

<p align="center">
  A local file converter. Images, audio, video, and documents — converted on your machine, with no cloud service and no AI.
</p>

---

## What It Does

Pick what you're converting, drop files onto the window, and Hypercycle writes the results wherever you choose:

- **Routing screen** — the app opens on a mode picker; each conversion type is one button, and unavailable types say which engine they need
- **Whole-surface drop target** — once a mode is selected the entire window accepts files, with a click-to-browse fallback
- **Queue drawer** — slides open when work is added, toggleable, and shows per-file state without covering the drop surface
- **Category groups** — a mixed batch splits into groups, each owning its own target format, so one selector never has to apply to everything
- **Format pairs** — every row shows source and target as chips, updating live when a group's target changes
- **Capability probe** — engines are checked at launch, so an unavailable format is disabled up front rather than failing mid-batch
- **Never overwrites** — an existing output gets a numbered suffix, and source files are never modified or removed

## Design Philosophy

- **Zero frameworks** — Python + vanilla CSS + vanilla JS. No React, no Node, no bundler.
- **Fully portable** — every engine arrives as a pip wheel. Nothing to install separately, nothing resolved from `PATH`, nothing downloaded on first use.
- **No AI** — every conversion is deterministic. No model inference, no cloud calls, no ML runtime in the dependency tree. Same input and settings produce byte-identical output.
- **Offline by default** — local conversion makes no network requests at all.
- **Brutalist terminal aesthetic** — near-black background, monospace everywhere, accent colour inherited from the ecosystem palette.

## Quick Start

> **Requires Hyperkit.** The build reads shared CSS and JS from `.hyperspace/.hyperkit/` and fails with a `FileNotFoundError` if it is missing. Hyperkit must exist as a sibling of `.hypercycle/`.

```bash
pip install -r requirements.txt
cd .hypercycle
pythonw hypercycle.py
```

`hypercycle.py` runs `build.py` on launch, so edits to `assets/` are always reflected — there is no separate build step to remember.

## Installation

**Requirements:** Python 3.11+

```
pywebview>=5.0
pillow>=11.3
pillow-heif>=1.6.0
PyMuPDF>=1.25.0
```

Every entry is a self-contained wheel. AVIF support is native to Pillow from 11.3 onward, which is why no separate AVIF plugin is listed — `pillow-heif` supplies HEIC and HEIF only.

## Supported Formats

| Direction | Formats |
|-----------|---------|
| Read | png, jpg, jpeg, webp, tiff, tif, bmp, gif, ico, heic, heif, avif, svg, pdf |
| Write | png, jpg, jpeg, webp, tiff, tif, bmp, gif, ico, heic, heif, avif |

SVG and PDF are input-only: they rasterise through PyMuPDF, and producing a vector from a bitmap is not a meaningful conversion.

Audio, video, and document modes appear on the routing screen but are disabled until their engines land.

## Structure

```
.hypercycle/
├── hypercycle.py          # PyWebView entrypoint and the JS bridge API
├── build.py               # Concatenates Hyperkit + app CSS/JS into generated_html.py
├── engines.py             # Format registry, capability probe, category and mode tables
├── convert_manager.py     # Job queue, worker thread, conversion execution
├── requirements.txt
├── create-shortcut.ps1    # Builds Hypercycle.lnk for taskbar pinning
├── assets/
│   ├── shell.html         # HTML shell with {{CSS}} and {{JS_BLOCKS}} placeholders
│   ├── css/               # App-local styles; primitives come from Hyperkit
│   ├── js/                # Frontend logic
│   └── icons/             # Brand mark and generated .ico variants
└── test/
    └── selftest.py        # Verifies the engine stack; also a portability check
```

`generated_html.py` is a build artifact and is gitignored.

## Selftest

Run after relocating the directory or setting up on a new machine:

```bash
python test/selftest.py
```

It probes every engine, converts a generated file to each declared target, and asserts the invariants — sources preserved, no overwrites, unsupported targets failing cleanly. Exits non-zero on any failure.

## Dependencies

**Hyperkit** (`.hyperspace/.hyperkit/`) — shared design tokens, CSS primitives, and the Python logging helper. The build fails loudly if absent.

**Hypervisor** (`.hyperspace/.hypervisor/`) — supplies the live palette from `preferences.json`. Optional at runtime: without it the Hyperkit token defaults apply and conversion is unaffected.

## Notes

- The window's dark title bar and icon are applied through the Windows DWM API after the webview loads. Applying them earlier silently fails, because the window cannot be resolved by title until it exists.
- Palette changes made in Hypervisor are picked up when the window regains focus, so there is no need to restart after changing the accent.
