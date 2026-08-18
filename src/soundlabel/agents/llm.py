"""LLM-backed A&R and Critic agents (optional — ``pip install soundlabel[llm]``).

Drop-in replacements for the heuristic agents: same interfaces, same
structural guarantees. The Critic still receives a :class:`BlindBrief` —
swapping intelligence in does not swap the blindness out.

Cost discipline: these agents spend tokens, so nothing in the framework
selects them by default. The CLI requires an explicit ``--llm`` flag, in
the same spirit as ``--allow-paid`` for generation backends.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ..brief import BlindBrief, Brief
from ..catalog import Artist
from ..scoring import ScoreReport
from .anr import BPM_BY_STYLE, KEYS, STYLE_POOL
from .critic import Verdict

DEFAULT_MODEL = os.environ.get("SOUNDLABEL_MODEL", "claude-opus-5")

_BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "style_tag": {"type": "string", "enum": STYLE_POOL},
        "mood": {"type": "string"},
        "theme": {"type": "string"},
        "title_hint": {"type": "string"},
        "bpm": {"type": "integer"},
        "key": {"type": "string", "enum": KEYS},
        "mode": {"type": "string", "enum": ["major", "minor"]},
        "rationale": {"type": "string"},
    },
    "required": ["style_tag", "mood", "theme", "title_hint", "bpm", "key", "mode", "rationale"],
    "additionalProperties": False,
}

_VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["accept", "redo", "kill"]},
        "reasons": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["decision", "reasons"],
    "additionalProperties": False,
}


def _client():
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "LLM agents need the anthropic SDK: pip install 'soundlabel[llm]'"
        ) from exc
    return anthropic.Anthropic()


def _structured(client, model: str, prompt: str, schema: dict) -> dict:
    response = client.messages.create(
        model=model,
        max_tokens=2048,
        output_config={"format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": prompt}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("model declined the request")
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


class LLMANRAgent:
    """A&R with taste: reads the same inputs as the heuristic agent, but
    reasons about them instead of hashing them."""

    def __init__(self, model: str = DEFAULT_MODEL):
        self.model = model

    def write_brief(self, artist: Artist, history: dict, seed: int = 0) -> Brief:
        prompt = (
            "You are the A&R lead of an independent AI music label. Decide what "
            "this artist should record next.\n\n"
            f"Artist: {artist.name} (slug {artist.slug}), language {artist.language}.\n"
            f"Sonic profile: {artist.sonic_profile or 'not yet defined'}\n"
            f"Catalog history: {json.dumps(history, ensure_ascii=False)}\n\n"
            "House rules: avoid repeating the dominant style when it already "
            "dominates the catalog; if the last verdict was a redo or kill, move "
            "away from that direction rather than retrying it harder. Pick a "
            "style_tag from the allowed list, a bpm that suits it, and a theme "
            "with a concrete image in it (not an abstraction)."
        )
        data = _structured(_client(), self.model, prompt, _BRIEF_SCHEMA)
        style = data["style_tag"]
        return Brief(
            artist_slug=artist.slug,
            style_tags=[style],
            mood=data["mood"],
            theme=data["theme"],
            title_hint=data["title_hint"],
            bpm=float(data.get("bpm") or BPM_BY_STYLE.get(style, 100)),
            key=data["key"],
            mode=data["mode"],
            bars=8,
            seed=seed,
        )


class LLMCriticAgent:
    """Blind critic: judges from the score report and creative intent only.

    The signature is the contract — ``BlindBrief`` carries no generation
    parameters, so neither does the prompt. The audio itself stays local;
    what the model sees is the measured evidence, which is also what keeps
    this affordable enough to run on every track.
    """

    def __init__(self, model: str = DEFAULT_MODEL):
        self.model = model

    def review(self, audio_path: Path, report: ScoreReport, brief: BlindBrief) -> Verdict:
        if not report.gate_passed:
            return Verdict("kill", 0.0, ["failed acceptance gate"] + report.gate_reasons)
        prompt = (
            "You are the independent Critic of a music label. You never see how "
            "a track was produced — only what it was meant to be and what the "
            "measurements say. Decide: accept (release it), redo (has a core "
            "worth keeping, needs another pass), or kill (not worth salvaging).\n\n"
            f"Creative intent: {brief.to_json()}\n"
            f"Automated score: {report.rank_score}/10 via {report.scorer}\n"
            f"Measurement detail: {json.dumps(report.detail, ensure_ascii=False, default=str)}\n\n"
            "Be specific in your reasons; cite the measurements you weighed. "
            "Automated scores are guardrails, not gold — treat a mid score with "
            "strong intent-fit as a redo candidate, not an automatic kill."
        )
        data = _structured(_client(), self.model, prompt, _VERDICT_SCHEMA)
        return Verdict(data["decision"], report.rank_score, list(data["reasons"]))
