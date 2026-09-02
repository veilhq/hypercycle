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


def main() -> int:
    failures = 0

    print("Engine capabilities")
    for cap in engines.capabilities():
        mark = PASS if cap.available else FAIL
        detail = f"  ({cap.detail})" if cap.detail else ""
        print(f"{mark}{cap.name}{detail}")
        if not cap.available:
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

    print()
    if failures:
        print(f"{failures} failure(s)")
    else:
        print("All checks passed.")
    return 1 if failures else 0


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
