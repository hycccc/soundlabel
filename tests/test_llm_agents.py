"""LLM agent tests.

The structural tests always run; the live test runs only when an API key is
present (it costs a few hundred tokens and is skipped in CI).
"""

import inspect
import os

import pytest

anthropic = pytest.importorskip("anthropic")

from soundlabel.agents.llm import LLMANRAgent, LLMCriticAgent  # noqa: E402
from soundlabel.brief import BlindBrief  # noqa: E402
from soundlabel.catalog import Artist  # noqa: E402
from soundlabel.scoring import ScoreReport  # noqa: E402


def test_llm_critic_signature_is_blind():
    """The LLM critic's contract is the same as the heuristic one: its brief
    parameter is annotated BlindBrief, so generation params cannot reach it."""
    sig = inspect.signature(LLMCriticAgent.review)
    assert sig.parameters["brief"].annotation in (BlindBrief, "BlindBrief")


def test_llm_agents_are_not_defaults():
    """Nothing in the pipeline imports the LLM agents implicitly — token
    spend stays opt-in."""
    import soundlabel.pipeline as pipeline
    src = inspect.getsource(pipeline)
    assert "llm" not in src.lower()


@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="no API key")
def test_llm_agents_live(tmp_path):
    artist = Artist("nova", "Nova Lin", "en", "electronic, rnb, late-night")
    brief = LLMANRAgent().write_brief(artist, {"n_tracks": 0, "style_counts": {},
                                               "recent_verdicts": []})
    assert brief.artist_slug == "nova" and brief.style_tags and brief.theme

    report = ScoreReport(True, [], 5.6, "lite",
                         {"rms_db": -16.2, "centroid_hz": 1400, "crest_db": 9.1,
                          "stereo_width": 0.4})
    verdict = LLMCriticAgent().review(tmp_path / "x.wav", report, brief.blind())
    assert verdict.decision in {"accept", "redo", "kill"} and verdict.reasons
