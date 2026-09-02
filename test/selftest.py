"""Hypercycle selftest — verifies the engine stack on this machine.

Doubles as a portability check: run this after relocating the app directory or
setting it up on a new machine to confirm every declared format actually works,
rather than discovering a gap mid-batch.

Usage:
    python test/selftest.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# Lives in test/, so the app modules are one level up.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import engines  # noqa: E402
from convert_manager import ConvertManager, Status  # noqa: E402

PASS = "  ok   "
FAIL = " FAIL  "
SKIP = " skip  "


def main() -> int:
    failures = 0

    print("Engine capabilities")
    # The PDF output engine is an opportunistic extra — nothing bundles it, and
    # its absence is a documented, expected state, not a suite failure. Every
    # other engine ships as a wheel and is expected present.
    optional = {"PDF output engine"}
    for cap in engines.capabilities():
        if cap.available:
            mark = PASS
        elif cap.name in optional:
            mark = SKIP
        else:
            mark = FAIL
        detail = f"  ({cap.detail})" if cap.detail else ""
        print(f"{mark}{cap.name}{detail}")
        if not cap.available and cap.name not in optional:
            failures += 1

    print()
    print(f"Readable: {', '.join(engines.readable_extensions())}")
    print(f"Targets:  {', '.join(engines.target_extensions())}")
    print()

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        out_dir = tmpdir / "out"
        out_dir.mkdir()

        sources = _make_sources(tmpdir)
        updates: list[dict] = []
        mgr = ConvertManager(on_update=updates.append)
        mgr.set_output_dir(str(out_dir))

        print("Conversions")
        expected = 0
        for src in sources:
            for tgt in ("png", "jpg", "webp", "heic", "avif", "ico"):
                if tgt not in engines.targets_for(src.suffix):
                    continue
                mgr.add(str(src), tgt)
                expected += 1

        started = mgr.start()
        if not started.get("ok"):
            print(f"{FAIL}queue did not start: {started.get('error')}")
            return 1

        mgr._worker.join(timeout=120)  # noqa: SLF001 - selftest drains directly

        for job in mgr.jobs():
            name = f"{Path(job['source']).name} -> .{job['target_ext']}"
            if job["status"] == Status.COMPLETE.value:
                out = Path(job["output"])
                if out.exists() and out.stat().st_size > 0:
                    print(f"{PASS}{name}  ({out.stat().st_size} bytes)")
                else:
                    print(f"{FAIL}{name}  reported complete but output missing/empty")
                    failures += 1
            else:
                print(f"{FAIL}{name}  {job['status']}: {job['error']}")
                failures += 1

        print()
        print("Invariants")
        for src in sources:
            if src.exists():
                print(f"{PASS}source preserved: {src.name}")
            else:
                print(f"{FAIL}source was removed: {src.name}")
                failures += 1

        # Overwrite protection: same job twice must not clobber.
        before = sorted(p.name for p in out_dir.iterdir())
        mgr.clear_finished()
        mgr.add(str(sources[0]), "png")
        mgr.start()
        mgr._worker.join(timeout=60)  # noqa: SLF001
        after = sorted(p.name for p in out_dir.iterdir())
        if len(after) == len(before) + 1:
            print(f"{PASS}repeat convert wrote a new file instead of overwriting")
        else:
            print(f"{FAIL}overwrite protection: {len(before)} -> {len(after)} files")
            failures += 1

        # Unsupported target must fail cleanly, not crash.
        mgr.clear_finished()
        mgr.add(str(sources[0]), "xyz")
        mgr.start()
        mgr._worker.join(timeout=30)  # noqa: SLF001
        bad = [j for j in mgr.jobs() if j["target_ext"] == "xyz"]
        if bad and bad[0]["status"] == Status.FAILED.value and bad[0]["error"]:
            print(f"{PASS}unsupported target failed cleanly: {bad[0]['error']}")
        else:
            print(f"{FAIL}unsupported target did not fail cleanly: {bad}")
            failures += 1

        failures += _check_media(tmpdir, out_dir)
        failures += _check_documents(tmpdir, out_dir)

    print()
    if failures:
        print(f"{failures} failure(s)")
    else:
        print("All checks passed.")
    return 1 if failures else 0


def _run_batch(out_dir: Path, pairs: list[tuple[Path, str]], timeout: int = 180):
    """Run a set of (source, target) conversions through a fresh manager and
    return the finished job dicts keyed by target extension."""
    mgr = ConvertManager(on_update=lambda d: None)
    mgr.set_output_dir(str(out_dir))
    for src, tgt in pairs:
        mgr.add(str(src), tgt)
    started = mgr.start()
    if not started.get("ok"):
        return None, started.get("error")
    mgr._worker.join(timeout=timeout)  # noqa: SLF001
    return {j["target_ext"]: j for j in mgr.jobs()}, None


def _check_media(tmpdir: Path, out_dir: Path) -> int:
    """Audio and video conversion via the bundled ffmpeg.

    Skipped (not failed) when ffmpeg did not resolve, so the suite still passes
    on a machine without the audio/video engine.
    """
    print()
    print("Audio & video (ffmpeg)")
    if not engines._HAS_FFMPEG:  # noqa: SLF001
        print(f"{SKIP}ffmpeg unavailable — audio/video checks skipped")
        return 0

    import subprocess

    exe = engines._FFMPEG_EXE  # noqa: SLF001
    failures = 0

    # Synthesize a short tone and a short test-pattern clip as sources.
    wav = tmpdir / "tone.wav"
    subprocess.run(
        [exe, "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-y", str(wav)],
        capture_output=True,
    )
    mp4 = tmpdir / "clip.mp4"
    subprocess.run(
        [exe, "-f", "lavfi", "-i", "testsrc=duration=2:size=160x120:rate=15",
         "-y", str(mp4)],
        capture_output=True,
    )

    pairs = []
    if "mp3" in engines.targets_for("wav"):
        pairs.append((wav, "mp3"))
    if "flac" in engines.targets_for("wav"):
        pairs.append((wav, "flac"))
    if "webm" in engines.targets_for("mp4"):
        pairs.append((mp4, "webm"))
    if "mkv" in engines.targets_for("mp4"):
        pairs.append((mp4, "mkv"))

    jobs, err = _run_batch(out_dir, pairs)
    if jobs is None:
        print(f"{FAIL}media batch did not start: {err}")
        return 1

    for tgt, job in jobs.items():
        name = f"{Path(job['source']).name} -> .{tgt}"
        out = Path(job["output"]) if job["output"] else None
        if job["status"] == Status.COMPLETE.value and out and out.exists() and out.stat().st_size > 0:
            print(f"{PASS}{name}  ({out.stat().st_size} bytes)")
        else:
            print(f"{FAIL}{name}  {job['status']}: {job['error']}")
            failures += 1

    # Sources must survive.
    for f in (wav, mp4):
        if not f.exists():
            print(f"{FAIL}source was removed: {f.name}")
            failures += 1

    return failures


def _check_documents(tmpdir: Path, out_dir: Path) -> int:
    """Document conversion via the bundled pandoc, plus the missing-PDF-engine
    message. Skipped (not failed) when pandoc did not resolve."""
    print()
    print("Documents (pandoc)")
    if not engines._HAS_PANDOC:  # noqa: SLF001
        print(f"{SKIP}pandoc unavailable — document checks skipped")
        return 0

    failures = 0

    md = tmpdir / "doc.md"
    md.write_text(
        "# Heading\n\nA paragraph with **bold** and a list:\n\n- one\n- two\n",
        encoding="utf-8",
    )

    pairs = []
    for tgt in ("html", "rtf", "docx", "odt"):
        if tgt in engines.targets_for("md"):
            pairs.append((md, tgt))

    jobs, err = _run_batch(out_dir, pairs)
    if jobs is None:
        print(f"{FAIL}document batch did not start: {err}")
        return 1

    for tgt, job in jobs.items():
        name = f"{Path(job['source']).name} -> .{tgt}"
        out = Path(job["output"]) if job["output"] else None
        if job["status"] == Status.COMPLETE.value and out and out.exists() and out.stat().st_size > 0:
            print(f"{PASS}{name}  ({out.stat().st_size} bytes)")
        else:
            print(f"{FAIL}{name}  {job['status']}: {job['error']}")
            failures += 1

    # HTML output should carry the converted content, not an empty shell.
    html_out = out_dir / "doc.html"
    if html_out.exists():
        txt = html_out.read_text(encoding="utf-8", errors="replace")
        if "Heading" in txt and "<strong>" in txt:
            print(f"{PASS}html output carries converted heading and bold markup")
        else:
            print(f"{FAIL}html output missing expected converted content")
            failures += 1

    # PDF output: either it works (a PDF engine is present) or it is refused
    # with an actionable message naming the missing engine. A silent failure or
    # a crash is the only wrong outcome.
    if engines._PDF_ENGINE:  # noqa: SLF001
        jobs, _ = _run_batch(out_dir, [(md, "pdf")])
        pdf_job = jobs.get("pdf") if jobs else None
        if pdf_job and pdf_job["status"] == Status.COMPLETE.value:
            print(f"{PASS}md -> .pdf via {engines._PDF_ENGINE}")  # noqa: SLF001
        else:
            print(f"{FAIL}md -> .pdf failed despite engine {engines._PDF_ENGINE}: "  # noqa: SLF001
                  f"{pdf_job['error'] if pdf_job else 'no job'}")
            failures += 1
    else:
        try:
            engines.plan(md, "pdf")
            print(f"{FAIL}md -> .pdf should have been refused with no PDF engine")
            failures += 1
        except ValueError as ex:
            if "PDF engine" in str(ex):
                print(f"{PASS}md -> .pdf refused with actionable message")
            else:
                print(f"{FAIL}md -> .pdf refused but message not actionable: {ex}")
                failures += 1

    if not md.exists():
        print(f"{FAIL}source was removed: {md.name}")
        failures += 1

    return failures


def _make_sources(tmpdir: Path) -> list[Path]:
    """Build one source file per input family we claim to support."""
    from PIL import Image

    sources: list[Path] = []

    raster = tmpdir / "raster.png"
    img = Image.new("RGBA", (80, 60), (40, 160, 220, 255))
    img.save(raster)
    sources.append(raster)

    alpha = tmpdir / "alpha.png"
    Image.new("RGBA", (48, 48), (255, 0, 0, 96)).save(alpha)
    sources.append(alpha)

    svg = tmpdir / "vector.svg"
    svg.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40">'
        "<style>.cls-1{fill:#3aa0dc;}</style>"
        '<rect class="cls-1" x="4" y="4" width="32" height="32"/>'
        "</svg>",
        encoding="utf-8",
    )
    sources.append(svg)

    return sources


if __name__ == "__main__":
    raise SystemExit(main())
