"""Hypercycle conversion manager — runs a queue of conversions off the UI thread.

Phase 1 conversions are in-process and fast, so each file reports as an
indeterminate state rather than a percentage. The worker-thread and callback
shape mirrors Hyperline's PTY manager so that ffmpeg subprocess conversions can
report real percentages later without reworking the queue or the frontend.

Source files are opened read-only and never modified or removed.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import engines

logger = logging.getLogger("hypercycle")

# ffmpeg prints the input duration to stderr as `Duration: HH:MM:SS.cc` when it
# opens a file, and streams `out_time_us=<microseconds>` to stdout under
# `-progress pipe:1`. Percentage is the ratio of the two — no ffprobe needed.
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2})(?:\.(\d+))?")
_OUT_TIME_US_RE = re.compile(r"out_time_us=(\d+)")


class _JobCancelled(Exception):
    """Raised inside a conversion when its subprocess was terminated by cancel().
    Distinguishes an operator cancel from a genuine engine failure."""


# Style overrides injected into pandoc's standalone Typst output before
# compiling, applied AFTER the template's own rules so they win. pandoc's
# template sets tables to `stroke: none` (no gridlines) and leaves headings at
# Typst's subtle defaults; these give tables real gridlines with a shaded, bold
# header row, and give headings a clear size hierarchy with breathing room.
_TYPST_STYLE_PREAMBLE = """
// --- Hypercycle document styling ---
#set table(
  stroke: 0.5pt + rgb("#c8c8c8"),
  inset: 7pt,
  fill: (_, row) => if row == 0 { rgb("#eeeeee") },
)
// Table cells read better left-aligned and unjustified — the template's
// centered, justified cells hyphenate and look ragged in narrow columns.
#show table.cell: set align(left + top)
#show table.cell: set par(justify: false)
#show table.cell.where(y: 0): strong

#show heading.where(level: 1): set text(size: 1.6em, weight: "bold")
#show heading.where(level: 2): set text(size: 1.3em, weight: "bold")
#show heading.where(level: 3): set text(size: 1.1em, weight: "bold")
#show heading: it => block(above: 1.2em, below: 0.6em, it)
// --- end Hypercycle styling ---
"""


class Status(str, Enum):
    PENDING = "pending"
    CONVERTING = "converting"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Job:
    job_id: int
    source: Path
    target_ext: str
    status: Status = Status.PENDING
    error: str = ""
    output: str = ""
    progress: float = 0.0

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "source": str(self.source),
            "name": self.source.name,
            "source_ext": self.source.suffix.lower().lstrip("."),
            "category": engines.category_for(self.source.suffix),
            "size": _human_size(self.source),
            "target_ext": self.target_ext,
            "status": self.status.value,
            "error": self.error,
            "output": self.output,
            "progress": self.progress,
        }


def _human_size(path: Path) -> str:
    """Byte count in the shortest readable unit. Empty when the file is gone."""
    try:
        n = float(path.stat().st_size)
    except OSError:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


class ConvertManager:
    """Owns the job queue and the worker thread that drains it."""

    def __init__(self, on_update):
        self._on_update = on_update
        self._jobs: dict[int, Job] = {}
        self._order: list[int] = []
        self._next_id = 1
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._cancelled: set[int] = set()
        self._output_dir: Path | None = None
        # Subprocess-backed jobs (ffmpeg) register their Popen here so cancel can
        # terminate the running engine, not just skip queued work.
        self._active_procs: dict[int, subprocess.Popen] = {}

    # -- queue management ---------------------------------------------------

    def set_output_dir(self, path: str | None) -> None:
        self._output_dir = Path(path) if path else None

    def add(self, source: str, target_ext: str) -> dict:
        src = Path(source)
        with self._lock:
            job = Job(job_id=self._next_id, source=src, target_ext=target_ext)
            self._jobs[job.job_id] = job
            self._order.append(job.job_id)
            self._next_id += 1
        self._emit(job)
        return job.to_dict()

    def jobs(self) -> list[dict]:
        with self._lock:
            return [self._jobs[i].to_dict() for i in self._order]

    def cancel(self, job_id: int) -> None:
        """Mark a job cancelled. A queued job is skipped before it starts; an
        in-flight subprocess conversion is terminated; in-flight in-process work
        (Pillow/MuPDF) finishes its current file since it cannot be interrupted
        mid-call and is fast enough not to matter."""
        proc = None
        with self._lock:
            self._cancelled.add(job_id)
            job = self._jobs.get(job_id)
            if job and job.status is Status.PENDING:
                job.status = Status.CANCELLED
            proc = self._active_procs.get(job_id)
        if proc and proc.poll() is None:
            # Terminate outside the lock — the reader thread needs the lock to
            # drain output, and terminate() can block briefly.
            try:
                proc.terminate()
            except Exception:
                logger.exception("failed to terminate subprocess for job %s", job_id)
        if job:
            self._emit(job)

    def retarget_category(self, category: str, target_ext: str) -> None:
        """Repoint still-pending jobs in one category at a new target format.

        Only pending work is touched — a job that already converted keeps the
        target it was actually written with, so the queue stays an honest record.
        """
        changed = []
        with self._lock:
            for job in self._jobs.values():
                if job.status is not Status.PENDING:
                    continue
                if engines.category_for(job.source.suffix) != category:
                    continue
                job.target_ext = target_ext
                changed.append(job)
        for job in changed:
            self._emit(job)

    def clear_finished(self) -> None:
        with self._lock:
            keep = [
                i
                for i in self._order
                if self._jobs[i].status
                in (Status.PENDING, Status.CONVERTING)
            ]
            for i in set(self._order) - set(keep):
                self._jobs.pop(i, None)
            self._order = keep

    # -- execution ----------------------------------------------------------

    def start(self) -> dict:
        if not self._output_dir:
            return {"ok": False, "error": "No output directory chosen"}
        if self._worker and self._worker.is_alive():
            return {"ok": False, "error": "Already running"}
        self._worker = threading.Thread(
            target=self._drain, name="hypercycle-convert", daemon=True
        )
        self._worker.start()
        return {"ok": True}

    def _drain(self) -> None:
        while True:
            with self._lock:
                nxt = next(
                    (
                        self._jobs[i]
                        for i in self._order
                        if self._jobs[i].status is Status.PENDING
                    ),
                    None,
                )
            if nxt is None:
                return
            if nxt.job_id in self._cancelled:
                nxt.status = Status.CANCELLED
                self._emit(nxt)
                continue
            self._run_one(nxt)

    def _run_one(self, job: Job) -> None:
        job.status = Status.CONVERTING
        self._emit(job)
        try:
            out_path = self._convert(job)
            job.output = str(out_path)
            job.status = Status.COMPLETE
            job.progress = 1.0
            logger.info("converted %s -> %s", job.source.name, out_path.name)
        except _JobCancelled:
            # A subprocess conversion was terminated by cancel(); this is not a
            # failure and must not surface an engine error to the operator.
            job.status = Status.CANCELLED
            logger.info("conversion cancelled for %s", job.source.name)
        except Exception as exc:
            # Surface the engine's own message; a failed file must not stop the
            # rest of the queue.
            job.status = Status.FAILED
            job.error = str(exc)
            logger.warning("conversion failed for %s: %s", job.source, exc)
        finally:
            with self._lock:
                self._active_procs.pop(job.job_id, None)
        self._emit(job)

    def _convert(self, job: Job) -> Path:
        """Route a job to its engine. The plan decides which one."""
        assert self._output_dir is not None
        plan = engines.plan(job.source, job.target_ext)
        out_path = self._unique_path(
            self._output_dir / f"{job.source.stem}.{plan.target_ext}"
        )

        if plan.engine in ("pillow", "mupdf"):
            return self._convert_image(job, plan, out_path)
        if plan.engine == "ffmpeg":
            return self._convert_ffmpeg(job, plan, out_path)
        if plan.engine == "pandoc":
            return self._convert_pandoc(job, plan, out_path)
        if plan.engine == "openpyxl":
            return self._convert_xlsx(job, plan, out_path)
        raise ValueError(f"Unknown engine: {plan.engine}")

    def _convert_image(self, job: Job, plan, out_path: Path) -> Path:
        if plan.reader == "mupdf":
            img = self._load_via_mupdf(job.source)
        else:
            img = self._load_via_pillow(job.source)

        try:
            if engines.needs_flattening(plan.pil_format) and img.mode in (
                "RGBA",
                "LA",
                "P",
            ):
                img = self._flatten(img)
            elif img.mode == "P" and plan.pil_format != "GIF":
                img = img.convert("RGBA")

            save_kwargs: dict = {}
            if plan.pil_format == "ICO":
                # ICO caps at 256px per side.
                img.thumbnail((256, 256))
            img.save(out_path, format=plan.pil_format, **save_kwargs)
        finally:
            img.close()

        return out_path

    def _convert_ffmpeg(self, job: Job, plan, out_path: Path) -> Path:
        """Convert audio/video via the bundled ffmpeg, streaming real progress.

        Duration is read from ffmpeg's own stderr `Duration:` line rather than a
        separate ffprobe call — imageio-ffmpeg bundles ffmpeg only. Progress
        comes from `-progress pipe:1` on stdout, and the ratio of the two drives
        the percentage pushed to the frontend. A cancel terminates the process
        and is reported as cancelled, not failed.
        """
        exe = engines._FFMPEG_EXE
        if not exe:
            raise ValueError("ffmpeg engine is unavailable")

        cmd = [
            exe,
            "-hide_banner",
            "-nostdin",
            "-i",
            str(job.source),
        ]
        # An explicit codec avoids ffmpeg guessing wrong for the container.
        if plan.codec:
            stream_flag = "-c:v" if engines.category_for(job.source.suffix) == "video" else "-c:a"
            cmd += [stream_flag, plan.codec]
        cmd += ["-progress", "pipe:1", "-y", str(out_path)]

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        with self._lock:
            self._active_procs[job.job_id] = proc

        # ffmpeg writes Duration to stderr as it opens the input. Read stderr on
        # a daemon thread so a full pipe never deadlocks the stdout progress read.
        duration_us = [0.0]
        stderr_tail: list[str] = []

        def _read_stderr() -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                stderr_tail.append(line)
                if duration_us[0] == 0.0:
                    m = _DURATION_RE.search(line)
                    if m:
                        h, mm, ss, frac = m.groups()
                        secs = int(h) * 3600 + int(mm) * 60 + int(ss)
                        if frac:
                            secs += float(f"0.{frac}")
                        duration_us[0] = secs * 1_000_000

        err_thread = threading.Thread(target=_read_stderr, daemon=True)
        err_thread.start()

        assert proc.stdout is not None
        for line in proc.stdout:
            m = _OUT_TIME_US_RE.search(line)
            if m and duration_us[0] > 0:
                done = int(m.group(1))
                pct = max(0.0, min(1.0, done / duration_us[0]))
                # Only emit on a meaningful change to avoid flooding the bridge.
                if pct - job.progress >= 0.01:
                    job.progress = pct
                    self._emit(job)
            elif line.startswith("progress=end"):
                break

        proc.wait()
        err_thread.join(timeout=2)

        if job.job_id in self._cancelled:
            # Terminated by cancel(); clean up any partial output.
            out_path.unlink(missing_ok=True)
            raise _JobCancelled()

        if proc.returncode != 0:
            tail = "".join(stderr_tail[-8:]).strip()
            raise RuntimeError(f"ffmpeg failed: {tail or f'exit code {proc.returncode}'}")

        return out_path

    def _convert_pandoc(self, job: Job, plan, out_path: Path) -> Path:
        """Convert a document via the bundled pandoc.

        pandoc runs quickly enough to report as an indeterminate in-progress
        state rather than a percentage.

        PDF is the one target pandoc cannot finish alone. The bundled Typst
        engine has no PATH executable for pandoc's --pdf-engine to call, so the
        portable path is two steps: pandoc emits Typst markup, then the Typst
        Python API compiles that markup to PDF in-process. A PATH engine, when
        one is present instead, is passed straight through to pandoc.
        """
        import pypandoc

        # PDF via the bundled Typst engine — the two-step in-process path.
        if plan.pandoc_to == "pdf" and plan.pdf_engine == engines.PDF_ENGINE_TYPST_PY:
            return self._convert_pdf_via_typst(job, plan, out_path)

        extra_args: list[str] = []
        if plan.pandoc_to == "pdf" and plan.pdf_engine:
            # A real PDF engine on PATH (LaTeX, wkhtmltopdf, …) drives pandoc directly.
            extra_args.append(f"--pdf-engine={plan.pdf_engine}")

        pypandoc.convert_file(
            str(job.source),
            to=plan.pandoc_to,
            format=plan.pandoc_from,
            outputfile=str(out_path),
            extra_args=extra_args,
        )
        return out_path

    def _convert_pdf_via_typst(self, job: Job, plan, out_path: Path) -> Path:
        """document -> Typst markup (pandoc) -> PDF (typst.compile), in-process.

        Both steps are wheel-only with nothing on PATH, so PDF output stays as
        portable as every other conversion. The intermediate .typ and any
        extracted media are written under a temp dir and removed afterward.

        Fidelity note: this path preserves document *structure* (headings, lists,
        tables, emphasis, images) but not the source's exact visual presentation
        — fonts, spacing, colours, and page layout are normalised by pandoc and
        re-rendered with Typst's template. Faithful visual reproduction would
        need a Word-format renderer (LibreOffice/Word), which is not wheel-only.
        Two options lift the result well above pandoc's bare fragment:
          --standalone     emits a complete Typst doc with pandoc's template,
                           including real table styling and figure handling.
          --extract-media  pulls embedded images out of the .docx and references
                           them, so pictures survive instead of vanishing.
        A style preamble is then injected to override the template's borderless
        tables and subtle headings — see `_TYPST_STYLE_PREAMBLE`.
        """
        import re
        import tempfile

        import pypandoc
        import typst

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            typ_path = td_path / (job.source.stem + ".typ")
            media_dir = td_path / "media"
            pypandoc.convert_file(
                str(job.source),
                to="typst",
                format=plan.pandoc_from,
                outputfile=str(typ_path),
                # pandoc rejects backslashes in --extract-media on Windows, so
                # hand it a POSIX-style path; it resolves fine either way.
                extra_args=["--standalone", f"--extract-media={media_dir.as_posix()}"],
            )

            # pandoc writes extracted-image references as ABSOLUTE paths. Typst
            # sandboxes file access to a root and refuses an absolute component
            # (e.g. a `C:` drive), so rewrite each image("<abs>") to a path
            # relative to the temp dir and compile with root=temp dir. Images
            # then load from inside the sandbox.
            text = typ_path.read_text(encoding="utf-8")
            root_posix = td_path.as_posix()

            def _relativise(m: "re.Match") -> str:
                ref = m.group(1)
                rp = ref.replace("\\", "/")
                if rp.startswith(root_posix):
                    rp = rp[len(root_posix):].lstrip("/")
                return f'image("{rp}"'

            text = re.sub(r'image\("([^"]+)"', _relativise, text)

            # Inject the style preamble so it overrides pandoc's template. It has
            # to land AFTER the template's own #set/#show block but BEFORE any
            # content — Typst show/set rules only affect what follows them.
            # pandoc closes its preamble with `#show: doc => conf( ... )`, so
            # insert right after that call. This is the reliable anchor: keying
            # on the first `= ` heading fails on documents whose headings pandoc
            # emitted as bold text rather than Typst headings (e.g. numbered
            # "1. Overview" sections), which would push the preamble past the
            # tables and silently no-op it.
            conf_m = re.search(r"#show:\s*doc\s*=>\s*conf\(", text)
            if conf_m:
                # Find the matching close paren of the conf(...) call.
                depth = 0
                i = text.index("(", conf_m.start())
                end = i
                for end in range(i, len(text)):
                    if text[end] == "(":
                        depth += 1
                    elif text[end] == ")":
                        depth -= 1
                        if depth == 0:
                            break
                insert_at = text.index("\n", end) + 1 if "\n" in text[end:] else len(text)
                text = text[:insert_at] + _TYPST_STYLE_PREAMBLE + "\n" + text[insert_at:]
            else:
                # No pandoc template (unlikely with --standalone): fall back to
                # the first heading, else prepend so it still precedes content.
                heading_m = re.search(r"(?m)^=+ ", text)
                cut = heading_m.start() if heading_m else 0
                text = text[:cut] + _TYPST_STYLE_PREAMBLE + "\n" + text[cut:]

            typ_path.write_text(text, encoding="utf-8")

            typst.compile(str(typ_path), output=str(out_path), root=str(td_path))
        return out_path

    def _convert_xlsx(self, job: Job, plan, out_path: Path) -> Path:
        """xlsx -> csv via openpyxl, in-process.

        The FIRST worksheet only is exported — a workbook maps to one CSV, which
        is the common "give me this as CSV" case. read_only keeps memory flat on
        large books; data_only takes the last-computed value of a formula cell
        rather than the formula text.
        """
        import csv

        import openpyxl

        wb = openpyxl.load_workbook(str(job.source), read_only=True, data_only=True)
        try:
            ws = wb[wb.sheetnames[0]]
            # newline="" so the csv module controls line endings on every platform.
            with open(out_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                for row in ws.iter_rows(values_only=True):
                    # Normalise empty cells (None) to "" so rows stay rectangular.
                    writer.writerow(["" if v is None else v for v in row])
        finally:
            wb.close()
        return out_path

    @staticmethod
    def _load_via_pillow(source: Path):
        from PIL import Image

        img = Image.open(source)
        img.load()
        return img

    @staticmethod
    def _load_via_mupdf(source: Path, scale: int = 4):
        """Rasterise SVG or PDF through MuPDF at `scale` for a clean downsample."""
        import io

        import fitz
        from PIL import Image

        if source.suffix.lower() == ".svg":
            text = engines.inline_css_fills(source.read_text(encoding="utf-8"))
            doc = fitz.open(stream=text.encode("utf-8"), filetype="svg")
        else:
            doc = fitz.open(source)

        try:
            if doc.page_count == 0:
                raise ValueError("Document has no pages to rasterise")
            page = doc[0]
            pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=True)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            img.load()
            return img
        finally:
            doc.close()

    @staticmethod
    def _flatten(img):
        """Composite onto white for targets without an alpha channel."""
        from PIL import Image

        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        return bg

    @staticmethod
    def _unique_path(path: Path) -> Path:
        """Never overwrite an existing file; suffix with -1, -2, ... instead."""
        if not path.exists():
            return path
        stem, suffix, parent = path.stem, path.suffix, path.parent
        n = 1
        while True:
            candidate = parent / f"{stem}-{n}{suffix}"
            if not candidate.exists():
                return candidate
            n += 1

    def _emit(self, job: Job) -> None:
        try:
            self._on_update(job.to_dict())
        except Exception:
            logger.exception("failed to push job update to frontend")
