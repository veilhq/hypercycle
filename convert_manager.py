"""Hypercycle conversion manager — runs a queue of conversions off the UI thread.

Phase 1 conversions are in-process and fast, so each file reports as an
indeterminate state rather than a percentage. The worker-thread and callback
shape mirrors Hyperline's PTY manager so that ffmpeg subprocess conversions can
report real percentages later without reworking the queue or the frontend.

Source files are opened read-only and never modified or removed.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import engines

logger = logging.getLogger("hypercycle")


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
        """Mark a job cancelled. In-flight in-process work finishes its current
        file; queued work is skipped before it starts."""
        with self._lock:
            self._cancelled.add(job_id)
            job = self._jobs.get(job_id)
            if job and job.status is Status.PENDING:
                job.status = Status.CANCELLED
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
        except Exception as exc:
            # Surface the engine's own message; a failed file must not stop the
            # rest of the queue.
            job.status = Status.FAILED
            job.error = str(exc)
            logger.warning("conversion failed for %s: %s", job.source, exc)
        self._emit(job)

    def _convert(self, job: Job) -> Path:
        assert self._output_dir is not None
        plan = engines.plan(job.source, job.target_ext)
        out_path = self._unique_path(
            self._output_dir / f"{job.source.stem}.{plan.target_ext}"
        )

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
