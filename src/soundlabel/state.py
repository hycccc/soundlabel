"""Workspace state snapshot — the file half of the label↔ops contract.

The catalog is SQLite read by Python; the ops sidecar is Node. Rather than
teach the sidecar SQL (and pin both processes to one driver and one schema
version), the two sides talk through plain files on the shared workspace:

- Python exports ``state.json`` after every batch (and on ``init``) —
  roster, recent batches, catalog stats. The sidecar injects it into the
  agent's system prompt and serves it at ``GET /label/state``.
- The sidecar writes ``batches/<id>/ops-review.json`` when asked to review
  a batch (``POST /label/review``); ``soundlabel batches`` displays it.

Either side can be restarted, upgraded, or absent without breaking the
other — the files are the API.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from pathlib import Path

from .agents.anr import COLD_ROOM_AVG, LOVED_ROOM_AVG, MIN_ROOM_SCORES
from .catalog import Catalog

STATE_VERSION = 1


def export_state(workspace: str | Path) -> Path:
    """Write ``state.json`` from the current catalog. Returns its path."""
    workspace = Path(workspace)
    catalog = Catalog(workspace / "catalog.db")
    tracks = catalog.tracks()
    scores = [r["score"] for r in tracks]
    reception = catalog.room_reception()
    state = {
        "version": STATE_VERSION,
        "updated_at": time.time(),
        "artists": [asdict(a) for a in catalog.artists()],
        "tracks": {
            "count": len(tracks),
            "avg_score": round(sum(scores) / len(scores), 3) if scores else None,
            "recent": [
                {"id": r["id"], "title": r["title"], "artist": r["artist_slug"],
                 "score": r["score"], "verdict": r["verdict"]}
                for r in tracks[-5:][::-1]
            ],
        },
        "batches": [
            {"id": r["id"], "artist": r["artist_slug"], "backend": r["backend"],
             "status": r["status"]}
            for r in catalog.batches(limit=10)
        ],
        # full reception map (not just recent tracks) — the sidecar reviews
        # arbitrary batches, and a score that scrolled out of `recent`
        # must still be visible to it. This is the only serialization of
        # reception; `recent` entries deliberately don't repeat it.
        "room_reception": reception,
        # the reception policy travels with the data so the sidecar applies
        # the SAME thresholds as the A&R agent — anr.py is the single source
        "room_policy": {"min_scores": MIN_ROOM_SCORES, "cold_avg": COLD_ROOM_AVG,
                        "loved_avg": LOVED_ROOM_AVG},
    }
    catalog.close()
    path = workspace / "state.json"
    # atomic replace: the sidecar reads this file on its own schedule, and a
    # torn read would silently cost a review its room signal
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    os.replace(tmp, path)
    return path


def read_review(workspace: str | Path, batch_id: str) -> dict | None:
    """Read the sidecar's review for one batch, if it wrote one."""
    path = Path(workspace) / "batches" / batch_id / "ops-review.json"
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
