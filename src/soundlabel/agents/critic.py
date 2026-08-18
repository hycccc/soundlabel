"""The Critic: accept / redo / kill, decided blind.

The Critic receives the finished audio, its :class:`ScoreReport`, and a
:class:`BlindBrief` — creative intent only. It has no field, argument, or
import path through which generation parameters could reach it. If you swap
this heuristic for an LLM critic, keep the signature: the blindness *is*
the calibration.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..brief import BlindBrief
from ..scoring import ScoreReport

ACCEPT_THRESHOLD = 5.0
KILL_THRESHOLD = 2.5


@dataclass
class Verdict:
    decision: str            # "accept" | "redo" | "kill"
    score: float
    reasons: list[str]


class CriticAgent:
    def review(self, audio_path: Path, report: ScoreReport, brief: BlindBrief) -> Verdict:
        if not report.gate_passed:
            return Verdict("kill", 0.0, ["failed acceptance gate"] + report.gate_reasons)

        reasons = [f"rank {report.rank_score}/10 via {report.scorer}"]
        if report.rank_score >= ACCEPT_THRESHOLD:
            reasons.append(f"meets bar for '{brief.mood}' {'/'.join(brief.style_tags)} release")
            return Verdict("accept", report.rank_score, reasons)
        if report.rank_score >= KILL_THRESHOLD:
            weakest = self._weakest_component(report)
            if weakest:
                reasons.append(f"weakest dimension: {weakest} — address before resubmitting")
            return Verdict("redo", report.rank_score, reasons)
        reasons.append("below salvage threshold")
        return Verdict("kill", report.rank_score, reasons)

    @staticmethod
    def _weakest_component(report: ScoreReport) -> str | None:
        components = report.detail.get("components") or report.detail.get("dimensions")
        if not components:
            return None
        def value(v):
            return v.get("score", 0) if isinstance(v, dict) else v
        return min(components, key=lambda k: value(components[k]))
