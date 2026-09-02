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

# ffmpeg drives audio and video. The binary ships inside the imageio-ffmpeg
# wheel and is resolved through get_ffmpeg_exe() — never a PATH search — so the
# portability guarantee holds. Resolution is attempted at import so an ffmpeg
# that is installed-but-broken disables the category instead of failing a batch.
_FFMPEG_EXE: str | None = None
_HAS_FFMPEG = False
try:
    import imageio_ffmpeg

    _FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
    _HAS_FFMPEG = bool(_FFMPEG_EXE)
except Exception as exc:  # pragma: no cover - dependency missing
    imageio_ffmpeg = None  # type: ignore[assignment]
    _PROBE_ERRORS["ffmpeg"] = str(exc)

# pandoc drives document conversion. The _binary distribution bundles the
# pandoc executable; resolving its path here confirms the bundled binary is
# present rather than relying on a system pandoc.
_PANDOC_PATH: str | None = None
_HAS_PANDOC = False
try:
    import pypandoc

    _PANDOC_PATH = pypandoc.get_pandoc_path()
    _HAS_PANDOC = bool(_PANDOC_PATH)
except Exception as exc:  # pragma: no cover - dependency missing
    pypandoc = None  # type: ignore[assignment]
    _PROBE_ERRORS["pandoc"] = str(exc)


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

# Audio formats ffmpeg reads and writes. The value is the ffmpeg codec/muxer
# hint used to build the command; an empty string means "let ffmpeg pick the
# default encoder for this container", which covers the common cases.
_AUDIO_RW = {
    "mp3": "libmp3lame",
    "wav": "pcm_s16le",
    "flac": "flac",
    "ogg": "libvorbis",
    "aac": "aac",
    "m4a": "aac",
    "opus": "libopus",
}

# Video formats ffmpeg reads and writes. Value is the default video codec for
# the container. "gif" is a video *target* only — animated GIF out — while GIF
# as a still lives in the image tables, so it is intentionally not a video
# input here.
_VIDEO_RW = {
    "mp4": "libx264",
    "mkv": "libx264",
    "webm": "libvpx-vp9",
    "mov": "libx264",
    "avi": "mpeg4",
}

# Document formats pandoc handles. Curated to the set the design names rather
# than pandoc's full graph, so the UI stays legible. PDF is deliberately absent
# — it needs a separate PDF engine pandoc does not bundle and is gated below.
_DOC_READ = {"md", "markdown", "docx", "html", "htm", "rtf", "epub", "odt", "latex", "tex", "txt"}
_DOC_WRITE = {"md", "docx", "html", "rtf", "epub", "odt", "latex", "txt"}

# pandoc's own format identifiers differ from file extensions in a few cases.
_PANDOC_FORMAT = {
    "md": "markdown",
    "markdown": "markdown",
    "tex": "latex",
    "latex": "latex",
    "htm": "html",
    "html": "html",
    "txt": "plain",
}

# PDF document output needs a PDF engine that pandoc does not bundle. Detect one
# on PATH so the UI can offer PDF only when it will actually work, and name the
# missing engine clearly when it will not.
_PDF_ENGINES = ("pdflatex", "xelatex", "lualatex", "tectonic", "typst",
                "wkhtmltopdf", "weasyprint", "context", "pdfroff")


def _find_pdf_engine() -> str | None:
    """Return the first available pandoc PDF engine, or None.

    Portability note: none of these ship with the app, so PDF output is an
    opportunistic extra that lights up only if the operator happens to have a
    PDF engine installed. The absence of one is surfaced as an actionable
    message, never a silent failure.
    """
    import shutil

    for name in _PDF_ENGINES:
        if shutil.which(name):
            return name
    return None


_PDF_ENGINE = _find_pdf_engine()

# JPEG has no alpha channel; anything with transparency needs flattening first.
_NO_ALPHA = {"JPEG", "BMP"}

# Category each extension belongs to. The UI groups the queue by this, so a mixed
# batch can carry a different target per category rather than one global target
# that cannot apply to everything.
_CATEGORY_BY_EXT = {}
for _e in ("png", "jpg", "jpeg", "webp", "tiff", "tif", "bmp", "gif", "ico",
           "heic", "heif", "avif"):
    _CATEGORY_BY_EXT[_e] = "images"
for _e in ("svg", "pdf"):
    _CATEGORY_BY_EXT[_e] = "vector"
for _e in _AUDIO_RW:
    _CATEGORY_BY_EXT[_e] = "audio"
for _e in _VIDEO_RW:
    _CATEGORY_BY_EXT[_e] = "video"
for _e in _DOC_READ | _DOC_WRITE:
    _CATEGORY_BY_EXT[_e] = "documents"

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
    """A resolved source-to-target conversion.

    `engine` selects the execution path in the manager:
      - "pillow" / "mupdf": in-process image conversion
      - "ffmpeg": audio/video subprocess with progress streaming
      - "pandoc": document subprocess

    The image fields (`pil_format`, `reader`) are only meaningful for the image
    engines; the media fields (`codec`, `pandoc_from`, `pandoc_to`,
    `pdf_engine`) are only meaningful for their respective engines.
    """

    source: Path
    target_ext: str
    engine: str  # "pillow" | "mupdf" | "ffmpeg" | "pandoc"
    pil_format: str = ""
    reader: str = ""  # "pillow" or "mupdf" (image engines only)
    codec: str = ""  # ffmpeg default codec hint
    pandoc_from: str = ""
    pandoc_to: str = ""
    pdf_engine: str = ""
    options: dict = field(default_factory=dict)


def capabilities() -> list[Capability]:
    """Report which engines resolved. Drives the UI's disabled states."""
    caps = [
        Capability("Pillow (raster images)", _HAS_PILLOW, _PROBE_ERRORS.get("pillow", "")),
        Capability("pillow-heif (HEIC/HEIF)", _HAS_HEIF, _PROBE_ERRORS.get("pillow-heif", "")),
        Capability("Pillow native AVIF", _HAS_AVIF, "" if _HAS_AVIF else "Pillow built without AVIF"),
        Capability("PyMuPDF (SVG/PDF input)", _HAS_MUPDF, _PROBE_ERRORS.get("pymupdf", "")),
        Capability("ffmpeg (audio/video)", _HAS_FFMPEG, _PROBE_ERRORS.get("ffmpeg", "")),
        Capability("pandoc (documents)", _HAS_PANDOC, _PROBE_ERRORS.get("pandoc", "")),
        Capability(
            "PDF output engine",
            bool(_PDF_ENGINE),
            f"using {_PDF_ENGINE}" if _PDF_ENGINE
            else "no PDF engine on PATH — document-to-PDF unavailable",
        ),
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
    if _HAS_FFMPEG:
        exts |= set(_AUDIO_RW) | set(_VIDEO_RW)
    if _HAS_PANDOC:
        exts |= _DOC_READ
    return sorted(exts)


def target_extensions() -> list[str]:
    """Every extension the app can produce across all engines.

    UI target lists are built per-source via `targets_for`, which filters to the
    source's own category. This flat set is the union of everything writable and
    is used mainly for validation.
    """
    exts: set[str] = set()
    if _HAS_PILLOW:
        exts |= set(_RASTER_RW)
    if _HAS_HEIF:
        exts |= set(_HEIF_RW)
    if _HAS_AVIF:
        exts |= set(_AVIF_RW)
    if _HAS_FFMPEG:
        exts |= set(_AUDIO_RW) | set(_VIDEO_RW)
    if _HAS_PANDOC:
        exts |= set(_DOC_WRITE)
        if _PDF_ENGINE:
            exts.add("pdf")
    return sorted(exts)


def _image_targets() -> list[str]:
    exts: set[str] = set()
    if _HAS_PILLOW:
        exts |= set(_RASTER_RW)
    if _HAS_HEIF:
        exts |= set(_HEIF_RW)
    if _HAS_AVIF:
        exts |= set(_AVIF_RW)
    return sorted(exts)


def targets_for(source_ext: str) -> list[str]:
    """Valid targets for a given source, restricted to the source's category.

    Conversions stay within a category — an mp3 converts to other audio formats,
    a docx to other document formats — because cross-category conversion (audio
    out of a video, an image of a document page) is either a distinct feature or
    meaningless. The one exception is the image category, where raster, HEIF,
    and AVIF all interconvert and SVG/PDF rasterise into any of them. A no-op
    same-format target is always excluded.
    """
    src = _normalise_ext(source_ext)
    if src not in readable_extensions():
        return []

    category = category_for(src)

    if category in ("images", "vector"):
        canonical = _RASTER_RW.get(src) or _HEIF_RW.get(src) or _AVIF_RW.get(src)
        out = []
        for ext in _image_targets():
            # Skip aliases that resolve to the same encoder (jpg/jpeg, tif/tiff).
            if canonical and _format_for(ext) == canonical:
                continue
            out.append(ext)
        return out

    if category == "audio" and _HAS_FFMPEG:
        return sorted(e for e in _AUDIO_RW if e != src)

    if category == "video" and _HAS_FFMPEG:
        return sorted(e for e in _VIDEO_RW if e != src)

    if category == "documents" and _HAS_PANDOC:
        # Same-target and extension aliases that resolve to the same pandoc
        # format (md/markdown, tex/latex, htm/html) are dropped as no-ops.
        src_fmt = _PANDOC_FORMAT.get(src, src)
        out = []
        for ext in sorted(_DOC_WRITE):
            if _PANDOC_FORMAT.get(ext, ext) == src_fmt:
                continue
            out.append(ext)
        if _PDF_ENGINE:
            out.append("pdf")
        return sorted(out)

    return []


def plan(source: Path, target_ext: str) -> ConversionPlan:
    """Resolve a conversion, raising with a clear reason when unsupported."""
    src_ext = _normalise_ext(source.suffix)
    tgt_ext = _normalise_ext(target_ext)

    if src_ext not in readable_extensions():
        raise ValueError(f"Unsupported input format: .{src_ext}")

    category = category_for(src_ext)

    # -- images / vector: in-process Pillow or MuPDF -----------------------
    if category in ("images", "vector"):
        if tgt_ext not in _image_targets():
            raise ValueError(f"Unsupported output format: .{tgt_ext}")
        reader = "mupdf" if src_ext in _MUPDF_READ_ONLY else "pillow"
        return ConversionPlan(
            source=source,
            target_ext=tgt_ext,
            engine=reader,
            pil_format=_format_for(tgt_ext),
            reader=reader,
        )

    # -- audio / video: ffmpeg subprocess ----------------------------------
    if category in ("audio", "video"):
        if not _HAS_FFMPEG:
            raise ValueError("ffmpeg engine is unavailable")
        table = _AUDIO_RW if category == "audio" else _VIDEO_RW
        if tgt_ext not in table:
            raise ValueError(f"Unsupported {category} output format: .{tgt_ext}")
        return ConversionPlan(
            source=source,
            target_ext=tgt_ext,
            engine="ffmpeg",
            codec=table[tgt_ext],
        )

    # -- documents: pandoc subprocess --------------------------------------
    if category == "documents":
        if not _HAS_PANDOC:
            raise ValueError("pandoc engine is unavailable")
        if tgt_ext == "pdf":
            if not _PDF_ENGINE:
                raise ValueError(
                    "PDF output needs a PDF engine (e.g. Typst, wkhtmltopdf, or a "
                    "LaTeX install). None was found on PATH, so PDF is unavailable. "
                    "Install one and restart, or choose a different target."
                )
            return ConversionPlan(
                source=source,
                target_ext="pdf",
                engine="pandoc",
                pandoc_from=_PANDOC_FORMAT.get(src_ext, src_ext),
                pandoc_to="pdf",
                pdf_engine=_PDF_ENGINE,
            )
        if tgt_ext not in _DOC_WRITE:
            raise ValueError(f"Unsupported document output format: .{tgt_ext}")
        return ConversionPlan(
            source=source,
            target_ext=tgt_ext,
            engine="pandoc",
            pandoc_from=_PANDOC_FORMAT.get(src_ext, src_ext),
            pandoc_to=_PANDOC_FORMAT.get(tgt_ext, tgt_ext),
        )

    raise ValueError(f"No engine for category: {category}")


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
