#!/usr/bin/env python3
"""Hypercycle — local file conversion for the Hyper ecosystem.

Converts files on this machine with no cloud service, no network access, and no
model inference. Every engine is an in-process, wheel-only dependency so the app
stays portable: nothing to install separately, nothing looked up on PATH.

Phase 1 covers images — raster formats via Pillow, HEIC/HEIF via pillow-heif,
AVIF via Pillow's native support, and SVG/PDF input rasterised through PyMuPDF.

Architecture:
  Dropzone/Queue UI -> Bridge API -> ConvertManager -> engines -> disk
  Job updates are pushed back to JS as they happen.

Usage:
    pythonw hypercycle.py
"""

import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import webview

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

HYPERCYCLE_DIR = Path(__file__).parent.resolve()
HYPERSPACE_ROOT = HYPERCYCLE_DIR.parent
HYPERVISOR_DIR = HYPERSPACE_ROOT / ".hypervisor"
# Live theme state is owned by preferences.json. theme-defaults.json is retained
# only as a source of semantic colors and as a last-resort fallback — it is
# deprecated for accent/warm/cool/comp and goes stale on the first palette change.
PREFERENCES = HYPERVISOR_DIR / "preferences.json"
THEME_DEFAULTS = HYPERVISOR_DIR / "theme-defaults.json"
BRAND_SVG = HYPERVISOR_DIR / "assets" / "SVG" / "CYCLE.svg"
ICON_FILE = HYPERCYCLE_DIR / "hypercycle.ico"
# Native window chrome is set before any stylesheet loads, so it cannot read a
# design token. Mirrors the --bg token value to avoid a white flash on open.
WINDOW_BG = "#030305"

sys.path.insert(0, str(HYPERSPACE_ROOT / ".hyperkit" / "python"))
from hyper_logging import setup_logger  # noqa: E402
from palette import build_palette_oklch  # noqa: E402

logger = setup_logger("hypercycle")

# Auto-build on launch so edits to assets/ are always reflected.
subprocess.run(
    [sys.executable, str(HYPERCYCLE_DIR / "build.py")],
    cwd=str(HYPERCYCLE_DIR),
)

sys.path.insert(0, str(HYPERCYCLE_DIR))
from generated_html import HTML  # noqa: E402

import engines  # noqa: E402
from convert_manager import ConvertManager  # noqa: E402


# ---------------------------------------------------------------------------
# Window chrome (dark title bar + icon)
# ---------------------------------------------------------------------------


def _apply_window_chrome(title: str, icon_path: str):
    """Force a dark title bar and set the custom icon via the Windows DWM API."""
    import ctypes

    hwnd = ctypes.windll.user32.FindWindowW(None, title)
    if not hwnd:
        logger.warning(
            "window chrome skipped: no window titled %r yet — dark title bar "
            "and icon not applied",
            title,
        )
        return

    DWMWA_USE_IMMERSIVE_DARK_MODE = 20
    val = ctypes.c_int(1)
    hr = ctypes.windll.dwmapi.DwmSetWindowAttribute(
        hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(val), ctypes.sizeof(val)
    )
    if hr != 0:
        logger.warning("dark title bar request returned HRESULT 0x%08x", hr & 0xFFFFFFFF)
    else:
        logger.info("dark title bar applied")

    if not Path(icon_path).exists():
        logger.warning("window icon missing: %s", icon_path)
        return

    IMAGE_ICON = 1
    LR_LOADFROMFILE = 0x0010
    LR_DEFAULTSIZE = 0x0040
    WM_SETICON = 0x0080
    ICON_BIG = 1
    ICON_SMALL = 0

    # Load the small and large variants separately so Windows uses the correctly
    # sized frame from the multi-size .ico rather than scaling one of them.
    for wparam, dim in ((ICON_SMALL, 16), (ICON_BIG, 32)):
        hicon = ctypes.windll.user32.LoadImageW(
            0, icon_path, IMAGE_ICON, dim, dim, LR_LOADFROMFILE
        )
        if not hicon:
            hicon = ctypes.windll.user32.LoadImageW(
                0, icon_path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE
            )
        if hicon:
            ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, wparam, hicon)
        else:
            logger.warning("failed to load icon at %dpx from %s", dim, icon_path)
    logger.info("window icon applied from %s", icon_path)


def _read_theme() -> dict:
    """Resolve the live palette the same way Hypervisor does.

    Theme state lives in preferences.json — theme-defaults.json is deprecated for
    this purpose and goes stale as soon as the operator changes the accent, which
    is why it must not be the primary source.

    Resolution order for warm/cool/comp:
      1. The named gradient map, when it is one of the operator's saved custom
         maps, since those carry explicit values.
      2. Otherwise derive from the accent with the shared OKLCH palette builder,
         which is the same derivation Hypervisor applies for built-in presets.

    Semantic colors are not stored per-palette, so they fall back to
    theme-defaults.json and finally to the Hyperkit token defaults.
    """
    theme: dict = {}

    try:
        if PREFERENCES.exists():
            prefs = json.loads(PREFERENCES.read_text(encoding="utf-8"))
            accent = prefs.get("hypervisor-accent")
            if accent:
                theme["accent"] = accent
                map_key = prefs.get("hypervisor-gradient-map") or ""
                user_maps = prefs.get("userGradientMaps") or {}
                named = user_maps.get(map_key)

                if isinstance(named, dict) and named.get("warm"):
                    theme.update(
                        {
                            "warm": named.get("warm", accent),
                            "cool": named.get("cool", accent),
                            "comp": named.get("comp", accent),
                        }
                    )
                else:
                    mode = prefs.get("hypervisor-palette-mode") or "analogous"
                    try:
                        derived = build_palette_oklch(accent, mode)
                        theme.update(
                            {
                                "warm": derived.get("warm", accent),
                                "cool": derived.get("cool", accent),
                                "comp": derived.get("comp", accent),
                            }
                        )
                    except Exception as exc:
                        logger.warning("palette derivation failed: %s", exc)
        else:
            logger.warning("preferences missing: %s", PREFERENCES)
    except Exception as exc:
        logger.warning("preferences read failed: %s", exc)

    # Semantics only — never let the deprecated file supply accent/warm/cool/comp
    # unless preferences produced nothing at all.
    try:
        if THEME_DEFAULTS.exists():
            defaults = json.loads(THEME_DEFAULTS.read_text(encoding="utf-8"))
            if defaults.get("semantics"):
                theme.setdefault("semantics", defaults["semantics"])
            if not theme.get("accent"):
                for key in ("accent", "warm", "cool", "comp"):
                    if defaults.get(key):
                        theme.setdefault(key, defaults[key])
    except Exception as exc:
        logger.warning("theme defaults read failed: %s", exc)

    return {k: v for k, v in theme.items() if v}


def _brand_svg_inner() -> str:
    """Return the brand mark as inline SVG with fill driven by currentColor.

    Mirrors the eco-app icon loader: strip the wrapper, keep the viewBox, and let
    the surrounding element's color drive the fill.
    """
    if not BRAND_SVG.exists():
        logger.warning("brand svg missing: %s", BRAND_SVG)
        return ""
    text = BRAND_SVG.read_text(encoding="utf-8")
    m_vb = re.search(r'viewBox="([^"]+)"', text)
    m_inner = re.search(r"<svg[^>]*>(.*)</svg>", text, re.DOTALL)
    if not (m_vb and m_inner):
        return ""
    inner = re.sub(r">\s+<", "><", m_inner.group(1).strip())
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{m_vb.group(1)}" '
        f'fill="currentColor" aria-hidden="true">{inner}</svg>'
    )


# ---------------------------------------------------------------------------
# Bridge API
# ---------------------------------------------------------------------------


class Api:
    """Methods exposed to the frontend as window.pywebview.api.*"""

    def __init__(self):
        self._window = None
        self._manager = ConvertManager(on_update=self._push_job)
        # Target format per category, so a mixed batch is not forced to share one
        # target that cannot apply to all of it.
        self._group_targets: dict[str, str] = {}

    def bind(self, window):
        self._window = window

    # -- startup ---------------------------------------------------------

    def get_startup_state(self):
        caps = [
            {"name": c.name, "available": c.available, "detail": c.detail}
            for c in engines.capabilities()
        ]
        cats, order, defaults = self._category_info()
        return {
            "capabilities": caps,
            "accepted": engines.readable_extensions(),
            "targets": engines.target_extensions(),
            "categories": cats,
            "category_order": order,
            "default_targets": defaults,
            "brand_svg": _brand_svg_inner(),
            "theme": _read_theme(),
        }

    def _category_info(self):
        """Which categories have queueable inputs, and what each can produce."""
        readable = engines.readable_extensions()
        targets = engines.target_extensions()

        present = []
        for ext in readable:
            cat = engines.category_for(ext)
            if cat not in present:
                present.append(cat)

        order = [c for c in engines.CATEGORY_ORDER if c in present]

        cats = {}
        for cat in order:
            # Vector and PDF inputs rasterise, so their targets are the raster
            # set rather than anything vector-native.
            cats[cat] = {
                "label": engines.CATEGORY_LABELS.get(cat, cat),
                "targets": targets,
            }

        defaults = {cat: self._default_target(cat, targets) for cat in order}
        return cats, order, defaults

    @staticmethod
    def _default_target(category: str, targets: list) -> str:
        preferred = {"images": "png", "vector": "png"}.get(category, "png")
        return preferred if preferred in targets else (targets[0] if targets else "")

    def get_theme(self):
        """Re-read the palette so a change made in Hypervisor can be picked up
        without restarting. Called by the frontend when the window regains focus."""
        return _read_theme()

    def set_group_target(self, category, target_ext):
        """Change the target format for one category and retarget its queued jobs."""
        self._group_targets[str(category)] = str(target_ext)
        self._manager.retarget_category(str(category), str(target_ext))
        return {"ok": True}

    # -- file intake -----------------------------------------------------

    def pick_files(self):
        """Native multi-select file dialog, filtered to supported inputs."""
        if not self._window:
            return []
        exts = engines.readable_extensions()
        pattern = ";".join(f"*.{e}" for e in exts)
        result = self._window.create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=True,
            file_types=(f"Supported images ({pattern})", "All files (*.*)"),
        )
        # OPEN_DIALOG returns a sequence; SAVE_DIALOG returns a bare string.
        if not result:
            return []
        return [str(p) for p in result]

    def pick_output_dir(self):
        """Native folder picker for the conversion destination."""
        if not self._window:
            return None
        result = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        if not result:
            return None
        path = result[0] if isinstance(result, (list, tuple)) else result
        self._manager.set_output_dir(str(path))
        logger.info("output directory set: %s", path)
        return str(path)

    # -- queue -----------------------------------------------------------

    def add_jobs(self, paths):
        """Queue each path, assigning the target from its category's current
        selection. Unsupported inputs are reported, not silently dropped."""
        accepted = set(engines.readable_extensions())
        targets = engines.target_extensions()
        added, rejected = [], []
        for raw in paths or []:
            p = Path(raw)
            if not p.is_file():
                rejected.append({"path": raw, "reason": "not a file"})
                continue
            ext = p.suffix.lower().lstrip(".")
            if ext not in accepted:
                rejected.append({"path": raw, "reason": "unsupported input"})
                continue
            category = engines.category_for(ext)
            target = self._group_targets.get(category) or self._default_target(
                category, targets
            )
            added.append(self._manager.add(str(p), target))
        if rejected:
            logger.info("rejected %d input(s)", len(rejected))
        return {"added": added, "rejected": rejected}

    def list_jobs(self):
        return self._manager.jobs()

    def start_queue(self):
        return self._manager.start()

    def cancel_job(self, job_id):
        self._manager.cancel(int(job_id))
        return {"ok": True}

    def clear_finished(self):
        self._manager.clear_finished()
        return {"ok": True}

    # -- push ------------------------------------------------------------

    def _push_job(self, job: dict):
        """Send a job state change to the frontend."""
        if not self._window:
            return
        payload = json.dumps(job)
        try:
            self._window.evaluate_js(f"window.hcJobUpdate({payload})")
        except Exception:
            logger.exception("failed to push job update")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def on_loaded(window, api):
    """Apply native window chrome once the webview has finished loading.

    The DWM calls below resolve the window by its title, which is not registered
    the instant the webview starts — so this runs off the loaded event with a
    short delay on a background thread. Applying it too early silently no-ops:
    FindWindowW returns 0 and both the dark title bar and the icon are skipped.
    """

    def _init():
        api.bind(window)
        time.sleep(0.5)
        _apply_window_chrome("Hypercycle", str(ICON_FILE))

    threading.Thread(target=_init, daemon=True).start()


def main():
    title = "Hypercycle"
    api = Api()
    window = webview.create_window(
        title,
        html=HTML,
        js_api=api,
        width=900,
        height=680,
        min_size=(680, 520),
        background_color=WINDOW_BG,
    )
    api.bind(window)
    window.events.loaded += lambda: on_loaded(window, api)

    caps = engines.capabilities()
    for cap in caps:
        logger.info(
            "engine %s: %s%s",
            cap.name,
            "available" if cap.available else "UNAVAILABLE",
            f" ({cap.detail})" if cap.detail else "",
        )

    logger.info("hypercycle starting")
    webview.start()


if __name__ == "__main__":
    main()
