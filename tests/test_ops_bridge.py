"""Label↔ops wiring tests — the Python half of the file contract.

The sidecar (Node) reads ``state.json`` and writes
``batches/<id>/ops-review.json``; here we verify Python keeps its side:
every batch run refreshes the state snapshot, and ``soundlabel batches``
surfaces a review the sidecar left behind. The Node half is exercised
end-to-end in CI (boot the sidecar against a demo workspace, POST
/label/review, read it back with the CLI).
"""

import json

from soundlabel.catalog import Artist, Catalog
from soundlabel.cli import main
from soundlabel.pipeline import run_batch
from soundlabel.state import export_state, read_review


def _workspace_with_artist(tmp_path):
    ws = tmp_path / "label"
    catalog = Catalog(ws / "catalog.db")
    catalog.add_artist(Artist("ivy", "Ivy", "en", "folk, ballad"))
    catalog.close()
    return ws


def test_run_batch_exports_state(tmp_path):
    ws = _workspace_with_artist(tmp_path)
    result = run_batch(ws, "ivy", backend="mock")

    state = json.loads((ws / "state.json").read_text())
    assert state["version"] == 1
    assert [a["slug"] for a in state["artists"]] == ["ivy"]
    assert state["batches"][0]["id"] == result.batch_id
    assert state["batches"][0]["status"] == result.status
    if result.status == "released":
        assert state["tracks"]["count"] == 1
        assert state["tracks"]["recent"][0]["id"] == result.track_id


def test_export_state_on_empty_workspace(tmp_path):
    ws = tmp_path / "label"
    Catalog(ws / "catalog.db").close()
    export_state(ws)
    state = json.loads((ws / "state.json").read_text())
    assert state["tracks"] == {"count": 0, "avg_score": None, "recent": []}
    assert state["artists"] == [] and state["batches"] == []


def test_batches_surfaces_ops_review(tmp_path, capsys):
    ws = _workspace_with_artist(tmp_path)
    result = run_batch(ws, "ivy", backend="mock")

    # what the sidecar's heuristicReview() writes (see ops/src/label-bridge.mjs)
    review = {
        "batch_id": result.batch_id,
        "source": "heuristic",
        "status": result.status,
        "headline": "released — rank 7.1/10 (full), critic accepted",
        "notes": ["brief: night drive (folk)"],
        "action": "queue for a listening-room session before promo",
    }
    (ws / "batches" / result.batch_id / "ops-review.json").write_text(json.dumps(review))
    assert read_review(ws, result.batch_id) == review

    main(["-w", str(ws), "batches"])
    out = capsys.readouterr().out
    assert result.batch_id in out
    assert "ops[heuristic]: released — rank 7.1/10" in out
    assert "→ queue for a listening-room session" in out


def test_batches_without_reviews_is_quiet(tmp_path, capsys):
    ws = _workspace_with_artist(tmp_path)
    run_batch(ws, "ivy", backend="mock")
    main(["-w", str(ws), "batches"])
    assert "ops[" not in capsys.readouterr().out
