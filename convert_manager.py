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
        state rather than a percentage. PDF output additionally needs a PDF
        engine, passed through when the plan resolved one.
        """
        import pypandoc

        extra_args: list[str] = []
        if plan.pandoc_to == "pdf" and plan.pdf_engine:
            extra_args.append(f"--pdf-engine={plan.pdf_engine}")

        pypandoc.convert_file(
            str(job.source),
            to=plan.pandoc_to,
            format=plan.pandoc_from,
            outputfile=str(out_path),
            extra_args=extra_args,
        )
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
