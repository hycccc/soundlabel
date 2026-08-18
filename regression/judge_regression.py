#!/usr/bin/env python3
"""Judge regression: assert the LLM judge still discriminates.

Why this exists: in production, an early judge configuration rated a fixture
set spanning a commercial pro release, a mixed AI generation, and a raw AI
generation at identical 9-10s — zero standard deviation on four of six
dimensions. The judge wasn't judging; it was complimenting. A rubric overhaul
with real-audio anchors and an artifact checklist brought per-dimension stdev
from 0.0 to 0.45-1.30. This script guards that property: it scores a fixture
set and FAILS when dispersion collapses back toward the ceiling.

Usage:
  python3 judge_regression.py fixtures.json [--min-stdev 0.3] [--min-range 2.0]

fixtures.json: [{"label": "pro-release-A", "context": "pop ballad female vocal",
                 "path": "/abs/path.mp3"}, ...]
Pick fixtures that SHOULD score differently — at minimum one professional
release and one raw generation. If the judge can't separate those, it can't
separate anything.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from soundlabel.scoring.judge import score_aesthetic

DIMS = ["musicality", "vocal_quality", "arrangement", "emotional_impact"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("fixtures", help="fixtures.json path")
    ap.add_argument("--refs-dir", help="few-shot anchor directory")
    ap.add_argument("--min-stdev", type=float, default=0.3)
    ap.add_argument("--min-range", type=float, default=2.0)
    args = ap.parse_args()

    fixtures = json.loads(Path(args.fixtures).read_text())
    rows = []
    header = f"{'label':<26} " + " ".join(f"{d[:4]:>5}" for d in DIMS)
    print(header)
    print("-" * len(header))
    for fx in fixtures:
        r = score_aesthetic(fx["path"], context=fx.get("context", ""), refs_dir=args.refs_dir)
        sub = r.get("sub_scores", {})
        row = [sub.get(d) for d in DIMS]
        cells = " ".join(f"{v:>5}" if v is not None else "    -" for v in row)
        print(f"{fx['label']:<26} {cells}")
        rows.append(row)

    print("\nPer-dim dispersion (stdev = discriminating power):")
    failures = []
    for i, name in enumerate(DIMS):
        vals = [r[i] for r in rows if r[i] is not None]
        if len(vals) < 2:
            continue
        stdev = statistics.stdev(vals)
        rng = max(vals) - min(vals)
        flags = ""
        if stdev < args.min_stdev:
            flags += " ❌ stdev<min"
            failures.append(name)
        if rng < args.min_range:
            flags += " ❌ range<min"
            failures.append(name)
        print(f"  {name}: n={len(vals)} min={min(vals)} max={max(vals)} stdev={stdev:.2f}{flags}")

    if failures:
        print(f"\nREGRESSION: dispersion collapse suspected in {sorted(set(failures))}")
        return 1
    print("\nPASS: the judge still discriminates")
    return 0


if __name__ == "__main__":
    sys.exit(main())
