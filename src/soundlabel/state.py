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
import time
from dataclasses import asdict
from pathlib import Path

from .catalog import Catalog

STATE_VERSION = 1


def export_state(workspace: str | Path) -> Path:
    """Write ``state.json`` from the current catalog. Returns its path."""
    workspace = Path(workspace)
    catalog = Catalog(workspace / "catalog.db")
    tracks = catalog.tracks()
    scores = [r["score"] for r in tracks]
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
    }
    catalog.close()
    path = workspace / "state.json"
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    return path


def read_review(workspace: str | Path, batch_id: str) -> dict | None:
    """Read the sidecar's review for one batch, if it wrote one."""
    path = Path(workspace) / "batches" / batch_id / "ops-review.json"
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
