"""Composite scoring: weight the tiers, keep the reasons."""

from __future__ import annotations

from . import dimensions
from .features import extract_features

WEIGHTS_WITH_AESTHETIC = {
    "production": 0.15, "lyrics_fit": 0.10, "originality": 0.05,
    "melody_proxy": 0.15, "vocal_presence": 0.15, "aesthetic": 0.40,
}
WEIGHTS_LOCAL_ONLY = {
    "production": 0.25, "lyrics_fit": 0.15, "originality": 0.10,
    "melody_proxy": 0.25, "vocal_presence": 0.25,
}


def score_song(audio_path: str, lyrics: str = "", original_lyrics: str = "",
               genre: str = "pop", judge: bool = False,
               refs_dir: str | None = None, context: str = "") -> dict:
    """Run the full local stack (plus the LLM judge when judge=True)."""
    features = extract_features(audio_path)
    results = {
        "production": dimensions.score_production(features, genre),
        "lyrics_fit": dimensions.score_lyrics_fit(lyrics, features["duration_s"]),
        "originality": dimensions.score_originality(lyrics, original_lyrics),
        "melody_proxy": dimensions.score_melody_proxy(features),
        "vocal_presence": dimensions.score_vocal_presence(features),
    }
    weights = WEIGHTS_LOCAL_ONLY
    if judge:
        from .judge import score_aesthetic
        aesthetic = score_aesthetic(audio_path, context=context, refs_dir=refs_dir)
        if not aesthetic.get("skipped"):
            results["aesthetic"] = aesthetic
            weights = WEIGHTS_WITH_AESTHETIC
        else:
            results["aesthetic"] = aesthetic

    scored = {k: v for k, v in results.items() if not v.get("skipped")}
    composite = sum(scored[k]["score"] * weights[k] for k in weights if k in scored)
    norm = sum(weights[k] for k in weights if k in scored) or 1
    return {
        "composite": round(composite / norm, 2),
        "dimensions": results,
        "weights": {k: weights[k] for k in weights if k in scored},
        "features": features,
    }
