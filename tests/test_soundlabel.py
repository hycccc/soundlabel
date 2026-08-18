"""End-to-end and design-principle tests.

The two most important tests here guard principles, not features:
``test_critic_is_structurally_blind`` and ``test_paid_backend_refused``.
"""

import dataclasses
import json
from pathlib import Path

import pytest

from soundlabel.backends import MockBackend, get_backend, register
from soundlabel.backends.base import GenerationBackend, GenerationResult
from soundlabel.brief import BlindBrief, Brief
from soundlabel.catalog import Artist, Catalog, track_id
from soundlabel.pipeline import PaidBackendRefused, run_batch
from soundlabel.scoring import gate, score


@pytest.fixture
def workspace(tmp_path):
    catalog = Catalog(tmp_path / "catalog.db")
    catalog.add_artist(Artist("testa", "Test Artist", "en", "pop, electronic"))
    catalog.close()
    return tmp_path


# -- design principles -----------------------------------------------------

def test_critic_is_structurally_blind():
    """BlindBrief must carry no generation parameters, in any field."""
    blind = Brief(artist_slug="a", bpm=133.0, key="F#", mode="minor",
                  seed=42, bars=16).blind()
    payload = json.dumps(dataclasses.asdict(blind))
    for leak in ("bpm", "key", "mode", "seed", "bars", "133", "F#"):
        assert leak not in payload
    assert dataclasses.fields(BlindBrief) == tuple(
        f for f in dataclasses.fields(BlindBrief)
        if f.name in {"artist_slug", "style_tags", "mood", "theme"}
    )


def test_paid_backend_refused(workspace):
    class PaidBackend(GenerationBackend):
        name = "paid"
        def cost_estimate(self, brief):
            return 5.0
        def generate(self, brief, out_dir):  # pragma: no cover — must not run
            raise AssertionError("paid backend was called without opt-in")

    register("paid", PaidBackend)
    with pytest.raises(PaidBackendRefused):
        run_batch(workspace, "testa", backend="paid")


def test_unscored_track_cannot_enter_catalog(tmp_path):
    catalog = Catalog(tmp_path / "c.db")
    with pytest.raises(TypeError):
        catalog.add_track("a", "t", "x.wav")  # score/verdict are not optional


# -- components ------------------------------------------------------------

def test_mock_backend_generates_scoreable_audio(tmp_path):
    result = MockBackend().generate(Brief(artist_slug="a", style_tags=["rnb"]), tmp_path)
    assert result.audio_path.exists() and result.cost == 0.0
    passed, reasons = gate(result.audio_path)
    assert passed, reasons
    report = score(result.audio_path)
    assert 0 < report.rank_score <= 10


def test_track_id_is_content_addressed(tmp_path):
    a, b = tmp_path / "a.bin", tmp_path / "b.bin"
    a.write_bytes(b"same bytes")
    b.write_bytes(b"same bytes")
    assert track_id(a) == track_id(b)  # identity survives moves and renames


def test_backend_registry_unknown():
    with pytest.raises(KeyError, match="registered"):
        get_backend("no-such-backend")


# -- the full loop ---------------------------------------------------------

def test_full_batch_end_to_end(workspace):
    result = run_batch(workspace, "testa", backend="mock")
    assert result.status in {"released", "redo", "killed"}
    manifest = json.loads(Path(result.manifest_path).read_text())
    steps = [s["step"] for s in manifest["steps"]]
    assert steps[:4] == ["brief", "cost-check", "generate", "score"]
    assert "critic" in steps
    if result.status == "released":
        catalog = Catalog(workspace / "catalog.db")
        rows = catalog.tracks("testa")
        assert len(rows) == 1 and rows[0]["score"] > 0
        catalog.close()


def test_anr_reads_history(workspace):
    for _ in range(3):
        run_batch(workspace, "testa", backend="mock")
    catalog = Catalog(workspace / "catalog.db")
    history = catalog.history("testa")
    catalog.close()
    assert "style_counts" in history and "recent_verdicts" in history
