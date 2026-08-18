"""The production pipeline: brief → generate → gate → rank → critic → catalog.

Every run writes a ``manifest.json`` recording each step's outcome and
timing — the batch is debuggable after the fact without logs. Failed
generation retries a bounded number of times; nothing in this module loops
forever or spends money without an explicit ``allow_paid=True``.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .agents import ANRAgent, CriticAgent, Verdict
from .backends import GenerationBackend, get_backend
from .brief import Brief
from .catalog import Catalog
from .scoring import ScoreReport, score

GENERATE_RETRIES = 2


class PaidBackendRefused(RuntimeError):
    """Raised when a backend costs money and the operator did not opt in."""


@dataclass
class BatchResult:
    batch_id: str
    status: str                 # "released" | "redo" | "killed" | "failed"
    track_id: str | None
    audio_path: str | None
    verdict: Verdict | None
    report: ScoreReport | None
    manifest_path: str


def _step(manifest: list, name: str, started: float, **payload) -> None:
    manifest.append({"step": name, "elapsed_s": round(time.time() - started, 3), **payload})


def run_batch(
    workspace: str | Path,
    artist_slug: str,
    backend: str | GenerationBackend = "mock",
    brief: Brief | None = None,
    allow_paid: bool = False,
    anr_agent=None,
    critic_agent=None,
) -> BatchResult:
    workspace = Path(workspace)
    catalog = Catalog(workspace / "catalog.db")
    artist = catalog.get_artist(artist_slug)
    if artist is None:
        raise KeyError(f"artist {artist_slug!r} not in the roster — add them first")

    be = get_backend(backend) if isinstance(backend, str) else backend
    batch_id = f"batch_{uuid.uuid4().hex[:8]}"
    batch_dir = workspace / "batches" / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []
    started = time.time()

    # -- brief -------------------------------------------------------------
    if brief is None:
        brief = (anr_agent or ANRAgent()).write_brief(artist, catalog.history(artist_slug))
    _step(manifest, "brief", started, brief=json.loads(brief.to_json()))
    catalog.open_batch(batch_id, artist_slug, be.name, brief.to_json())

    def finish(status: str, track_id=None, audio_path=None, verdict=None, report=None) -> BatchResult:
        manifest_path = batch_dir / "manifest.json"
        manifest_path.write_text(json.dumps(
            {"batch_id": batch_id, "status": status, "steps": manifest},
            indent=2, ensure_ascii=False))
        catalog.close_batch(batch_id, status, str(manifest_path))
        catalog.close()
        return BatchResult(batch_id, status, track_id,
                           str(audio_path) if audio_path else None,
                           verdict, report, str(manifest_path))

    # -- cost check --------------------------------------------------------
    estimate = be.cost_estimate(brief)
    if estimate > 0 and not allow_paid:
        _step(manifest, "cost-check", started, refused=True, estimate=estimate)
        catalog.close_batch(batch_id, "refused")
        raise PaidBackendRefused(
            f"backend {be.name!r} estimates cost {estimate}; pass allow_paid=True "
            f"(CLI: --allow-paid) to spend it")
    _step(manifest, "cost-check", started, estimate=estimate)

    # -- generate (bounded retries) ---------------------------------------
    result = None
    for attempt in range(1 + GENERATE_RETRIES):
        try:
            result = be.generate(brief, batch_dir)
            _step(manifest, "generate", started, attempt=attempt,
                  audio=str(result.audio_path), params=result.params, cost=result.cost)
            break
        except Exception as exc:  # noqa: BLE001 — retrying any backend failure
            _step(manifest, "generate", started, attempt=attempt, error=str(exc))
            if attempt == GENERATE_RETRIES:
                return finish("failed")

    # -- score -------------------------------------------------------------
    genre = brief.style_tags[0] if brief.style_tags else "pop"
    report = score(result.audio_path, genre=genre)
    _step(manifest, "score", started, gate_passed=report.gate_passed,
          gate_reasons=report.gate_reasons, rank=report.rank_score, scorer=report.scorer)

    # -- critic (blind) ----------------------------------------------------
    verdict = (critic_agent or CriticAgent()).review(result.audio_path, report, brief.blind())
    _step(manifest, "critic", started, **asdict(verdict))

    # -- catalog -----------------------------------------------------------
    if verdict.decision == "accept":
        title = brief.title_hint or brief.theme.title()
        detail = {"style_tags": brief.style_tags, **{k: v for k, v in report.detail.items()
                                                    if k != "features"}}
        tid = catalog.add_track(artist_slug, title, result.audio_path,
                                report.rank_score, verdict.decision,
                                score_detail=detail, batch_id=batch_id)
        _step(manifest, "catalog", started, track_id=tid, title=title)
        return finish("released", tid, result.audio_path, verdict, report)

    status = "redo" if verdict.decision == "redo" else "killed"
    return finish(status, None, result.audio_path, verdict, report)
