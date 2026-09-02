"""Hypercycle engine registry — maps source and target formats to converters.

Phase 1 covers images only. Every engine here runs in-process via a wheel-only
dependency: no system installs, no PATH lookups, no download-on-first-use. That
constraint is deliberate and should hold as audio, video, and document support
land later.

Format support is declared, not inferred. Adding a format means editing the
tables below; nothing elsewhere in the app needs to change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Engine availability
# ---------------------------------------------------------------------------
# Probed once at import so the UI can disable categories up front rather than
# failing partway through a batch.

_PROBE_ERRORS: dict[str, str] = {}

try:
    from PIL import Image, features

    _HAS_PILLOW = True
except Exception as exc:  # pragma: no cover - dependency missing
    Image = None  # type: ignore[assignment]
    features = None  # type: ignore[assignment]
    _HAS_PILLOW = False
    _PROBE_ERRORS["pillow"] = str(exc)

# HEIC/HEIF arrive through pillow-heif. AVIF is native in Pillow 11.3+, which is
# why pillow-heif no longer exposes an AVIF opener — registering one would fail.
_HAS_HEIF = False
if _HAS_PILLOW:
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
        _HAS_HEIF = True
    except Exception as exc:  # pragma: no cover - dependency missing
        _PROBE_ERRORS["pillow-heif"] = str(exc)

_HAS_AVIF = bool(_HAS_PILLOW and features and features.check("avif"))

try:
    import fitz  # PyMuPDF

    _HAS_MUPDF = True
except Exception as exc:  # pragma: no cover - dependency missing
    fitz = None  # type: ignore[assignment]
    _HAS_MUPDF = False
    _PROBE_ERRORS["pymupdf"] = str(exc)


# ---------------------------------------------------------------------------
# Format tables
# ---------------------------------------------------------------------------

# Raster formats Pillow can both read and write.
_RASTER_RW = {
    "png": "PNG",
    "jpg": "JPEG",
    "jpeg": "JPEG",
    "webp": "WEBP",
    "tiff": "TIFF",
    "tif": "TIFF",
    "bmp": "BMP",
    "gif": "GIF",
    "ico": "ICO",
}

# Formats that need pillow-heif registered before Pillow recognises them.
_HEIF_RW = {"heic": "HEIF", "heif": "HEIF"}

# AVIF rides on Pillow's native support rather than a plugin.
_AVIF_RW = {"avif": "AVIF"}

# Vector and document inputs that rasterise through MuPDF. Read-only: producing
# an SVG from a bitmap is not a meaningful conversion, so these never appear as
# targets.
_MUPDF_READ_ONLY = {"svg", "pdf"}

# JPEG has no alpha channel; anything with transparency needs flattening first.
_NO_ALPHA = {"JPEG", "BMP"}

# Category each extension belongs to. The UI groups the queue by this, so a mixed
# batch can carry a different target per category rather than one global target
# that cannot apply to everything. Audio, video, and document categories join
# here as their engines land — nothing else needs to change to accommodate them.
_CATEGORY_BY_EXT = {}
for _e in ("png", "jpg", "jpeg", "webp", "tiff", "tif", "bmp", "gif", "ico",
           "heic", "heif", "avif"):
    _CATEGORY_BY_EXT[_e] = "images"
for _e in ("svg", "pdf"):
    _CATEGORY_BY_EXT[_e] = "vector"

# Display label and ordering for each category.
CATEGORY_LABELS = {
    "images": "Images",
    "vector": "Vector & PDF",
    "audio": "Audio",
    "video": "Video",
    "documents": "Documents",
    "fetch": "From URL",
}
CATEGORY_ORDER = ["images", "vector", "audio", "video", "documents", "fetch"]

# ---------------------------------------------------------------------------
# Conversion modes
# ---------------------------------------------------------------------------
# A mode is what the operator picks on the routing screen. It is coarser than a
# category: "Images" accepts raster files as well as SVG and PDF, because from
# the operator's point of view all of those are "an image I want converted".
# Adding a mode here adds a button to the routing screen — nothing else changes.

MODES = [
    {
        "id": "images",
        "label": "Images",
        "categories": ["images", "vector"],
        "blurb": "raster, vector, and PDF pages",
    },
    {
        "id": "audio",
        "label": "Audio",
        "categories": ["audio"],
        "blurb": "requires the ffmpeg engine",
    },
    {
        "id": "video",
        "label": "Video",
        "categories": ["video"],
        "blurb": "requires the ffmpeg engine",
    },
    {
        "id": "documents",
        "label": "Documents",
        "categories": ["documents"],
        "blurb": "requires the pandoc engine",
    },
    {
        "id": "fetch",
        "label": "From URL",
        "categories": ["fetch"],
        "blurb": "fetch media from a link",
    },
]


def modes() -> list[dict]:
    """Routing modes with availability resolved against what actually loaded.

    A mode is available only when at least one of its categories has a usable
    input extension, so the routing screen can never offer a dead end.
    """
    readable = readable_extensions()
    live_categories = {category_for(e) for e in readable}

    out = []
    for mode in MODES:
        available = any(c in live_categories for c in mode["categories"])
        exts = sorted(e for e in readable if category_for(e) in mode["categories"])
        out.append(
            {
                "id": mode["id"],
                "label": mode["label"],
                "blurb": mode["blurb"],
                "categories": mode["categories"],
                "available": available,
                "extensions": exts,
            }
        )
    return out


def mode_extensions(mode_id: str) -> list[str]:
    """Input extensions a given mode accepts."""
    for mode in MODES:
        if mode["id"] == mode_id:
            return sorted(
                e for e in readable_extensions() if category_for(e) in mode["categories"]
            )
    return []


def category_for(ext: str) -> str:
    """Which queue group an input belongs to."""
    return _CATEGORY_BY_EXT.get(_normalise_ext(ext), "images")


@dataclass(frozen=True)
class Capability:
    """One engine's availability, for the launch-time probe."""

    name: str
    available: bool
    detail: str = ""


@dataclass
class ConversionPlan:
    """A resolved source-to-target conversion."""

    source: Path
    target_ext: str
    pil_format: str
    reader: str  # "pillow" or "mupdf"
    options: dict = field(default_factory=dict)


def capabilities() -> list[Capability]:
    """Report which engines resolved. Drives the UI's disabled states."""
    caps = [
        Capability("Pillow (raster images)", _HAS_PILLOW, _PROBE_ERRORS.get("pillow", "")),
        Capability("pillow-heif (HEIC/HEIF)", _HAS_HEIF, _PROBE_ERRORS.get("pillow-heif", "")),
        Capability("Pillow native AVIF", _HAS_AVIF, "" if _HAS_AVIF else "Pillow built without AVIF"),
        Capability("PyMuPDF (SVG/PDF input)", _HAS_MUPDF, _PROBE_ERRORS.get("pymupdf", "")),
    ]
    return caps


def readable_extensions() -> list[str]:
    """Extensions the app can accept as input, given what actually loaded."""
    exts: set[str] = set()
    if _HAS_PILLOW:
        exts |= set(_RASTER_RW)
    if _HAS_HEIF:
        exts |= set(_HEIF_RW)
    if _HAS_AVIF:
        exts |= set(_AVIF_RW)
    if _HAS_MUPDF:
        exts |= _MUPDF_READ_ONLY
    return sorted(exts)


def target_extensions() -> list[str]:
    """Extensions the app can produce. Excludes read-only vector/document inputs."""
    exts: set[str] = set()
    if _HAS_PILLOW:
        exts |= set(_RASTER_RW)
    if _HAS_HEIF:
        exts |= set(_HEIF_RW)
    if _HAS_AVIF:
        exts |= set(_AVIF_RW)
    return sorted(exts)


def targets_for(source_ext: str) -> list[str]:
    """Valid targets for a given source, excluding a no-op same-format convert."""
    src = _normalise_ext(source_ext)
    if src not in readable_extensions():
        return []
    canonical = _RASTER_RW.get(src) or _HEIF_RW.get(src) or _AVIF_RW.get(src)
    out = []
    for ext in target_extensions():
        # Skip aliases that resolve to the same encoder (jpg/jpeg, tif/tiff).
        if canonical and _format_for(ext) == canonical:
            continue
        out.append(ext)
    return out


def plan(source: Path, target_ext: str) -> ConversionPlan:
    """Resolve a conversion, raising with a clear reason when unsupported."""
    src_ext = _normalise_ext(source.suffix)
    tgt_ext = _normalise_ext(target_ext)

    if src_ext not in readable_extensions():
        raise ValueError(f"Unsupported input format: .{src_ext}")
    if tgt_ext not in target_extensions():
        raise ValueError(f"Unsupported output format: .{tgt_ext}")

    reader = "mupdf" if src_ext in _MUPDF_READ_ONLY else "pillow"
    return ConversionPlan(
        source=source,
        target_ext=tgt_ext,
        pil_format=_format_for(tgt_ext),
        reader=reader,
    )


def needs_flattening(pil_format: str) -> bool:
    """True when the target cannot carry an alpha channel."""
    return pil_format in _NO_ALPHA


def _normalise_ext(ext: str) -> str:
    return ext.lower().lstrip(".")


def _format_for(ext: str) -> str:
    e = _normalise_ext(ext)
    for table in (_RASTER_RW, _HEIF_RW, _AVIF_RW):
        if e in table:
            return table[e]
    raise ValueError(f"No encoder for .{e}")


# ---------------------------------------------------------------------------
# SVG helpers
# ---------------------------------------------------------------------------

_CSS_RULE_RE = re.compile(r"\.([\w-]+)\s*\{([^}]*)\}")
_STYLE_BLOCK_RE = re.compile(r"<style[^>]*>(.*?)</style>", re.DOTALL)
_CLASSED_TAG_RE = re.compile(r'<[\w:]+\b[^>]*\bclass="([^"]*)"[^>]*/?>')


def inline_css_fills(svg_text: str) -> str:
    """Promote `.cls { fill: ... }` style rules to presentation attributes.

    MuPDF does not apply CSS class selectors, so Illustrator and Figma exports
    render every path with the default black fill unless the rules are inlined
    first. Shares its approach with the icon rasterisation tooling, including
    two details that are easy to get wrong: attributes must be inserted after
    the class value so a self-closing `/>` survives, and a property already
    present on the element must not be duplicated, since duplicate attributes
    are an XML syntax error.
    """
    style_blocks = _STYLE_BLOCK_RE.findall(svg_text)
    if not style_blocks:
        return svg_text

    rules: dict[str, dict[str, str]] = {}
    for block in style_blocks:
        for selector, body in _CSS_RULE_RE.findall(block):
            props: dict[str, str] = {}
            for decl in body.split(";"):
                if ":" not in decl:
                    continue
                key, _, value = decl.partition(":")
                props[key.strip()] = value.strip()
            if props:
                rules.setdefault(selector, {}).update(props)

    if not rules:
        return svg_text

    def _add_attrs(match: re.Match) -> str:
        tag = match.group(0)
        props: dict[str, str] = {}
        for cls in match.group(1).split():
            props.update(rules.get(cls, {}))
        if not props:
            return tag
        attrs = " ".join(
            f'{k}="{v}"'
            for k, v in props.items()
            if not re.search(rf"\b{re.escape(k)}\s*=", tag)
        )
        if not attrs:
            return tag
        idx = match.end(1) - match.start(0) + 1
        return tag[:idx] + " " + attrs + tag[idx:]

    return _CLASSED_TAG_RE.sub(_add_attrs, svg_text)
