"""LLM-judge tier: aesthetic scoring with anchored few-shot references.

Design notes, learned the hard way in production:

- Without anchors, small multimodal judges collapse to the ceiling: they rate
  a commercial pro release and a raw AI generation at identical 9s. The fix
  is few-shot anchoring with REAL audio at known score levels (high / mid /
  low), plus an artifact checklist in the rubric.
- Judge quality is a regression surface. Prompts drift, models get swapped —
  see regression/judge_regression.py, which fails CI when per-dimension score
  dispersion collapses.
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.request
from pathlib import Path

RUBRIC = """You are a professional A&R (Artists & Repertoire) judge evaluating AI-generated songs.
Listen to each audio clip and rate it on a 0-10 scale across these criteria.

## Scoring Rubric

**Musicality** (overall musical quality):
- 9-10: Immediately engaging, memorable melody, professional arrangement
- 7-8: Pleasant to listen to, coherent structure, minor rough spots
- 5-6: Functional but generic, lacks personality
- 3-4: Awkward melody or rhythm, noticeable issues
- 0-2: Unlistenable, major structural problems

**Vocal Quality** (naturalness and expression):
- 9-10: Indistinguishable from human, emotionally expressive
- 7-8: Natural sounding, minor AI artifacts
- 5-6: Clearly AI but acceptable, limited expression
- 3-4: Robotic, pronunciation errors, unnatural phrasing
- 0-2: Distorted, garbled, or completely wrong

**Arrangement** (instrumental coherence):
- 9-10: Rich, well-balanced, every instrument serves a purpose
- 7-8: Solid arrangement, good instrument choices
- 5-6: Basic arrangement, some instruments feel out of place
- 3-4: Sparse or cluttered, poor instrument selection
- 0-2: Chaotic, clashing instruments

**Emotional Impact** (does it move you?):
- 9-10: Genuinely moving, would share with others
- 7-8: Evokes intended emotion, good mood setting
- 5-6: Neutral, neither moving nor off-putting
- 3-4: Emotionally flat or confusing mood
- 0-2: Actively unpleasant or jarring

Listen for common generation artifacts before scoring: garbled or smeared
consonants, phantom instrument entrances, section transitions that jump-cut,
loops that drift out of key, endings that collapse. A track with any of these
cannot score above 7 on the affected dimension.

Return ONLY a JSON object: {"musicality": <float>, "vocal_quality": <float>, "arrangement": <float>, "emotional_impact": <float>, "summary": "<1-2 sentence assessment>"}
"""


def load_fewshot_refs(refs_dir: str | Path) -> list[dict]:
    """Load few-shot anchors from refs_dir/refs.json.

    refs.json format: [{"file": "high_example.mp3", "label": "...",
    "response": {"musicality": 9.0, ...}}]. Supply your own anchor clips —
    one high, one mid, one low is the minimum useful set.
    """
    refs_dir = Path(refs_dir)
    manifest = refs_dir / "refs.json"
    if not manifest.exists():
        return []
    turns = []
    for ref in json.loads(manifest.read_text()):
        path = refs_dir / ref["file"]
        if not path.exists():
            continue
        turns.append({"role": "user", "parts": [
            {"inline_data": {"mime_type": "audio/mp3",
                             "data": base64.b64encode(path.read_bytes()).decode()}},
            {"text": f"Rate this song. Context: {ref.get('label', '')}"}]})
        response = ref["response"]
        turns.append({"role": "model", "parts": [
            {"text": json.dumps(response) if isinstance(response, dict) else str(response)}]})
    return turns


def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0)) if m else {}


def score_aesthetic(audio_path: str, context: str = "",
                    refs_dir: str | Path | None = None,
                    model: str | None = None, api_key: str | None = None) -> dict:
    """Score one track with the LLM judge (Gemini API by default).

    Env: GEMINI_API_KEY (required), SONGSCORE_JUDGE_MODEL (optional).
    Returns {"score": 0-10, "sub_scores": {...}, "summary": str, "issues": []}.
    """
    api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return {"score": 0.0, "issues": ["No GEMINI_API_KEY configured"], "skipped": True}
    audio = Path(audio_path)
    if not audio.exists():
        return {"score": 0.0, "issues": [f"Audio file not found: {audio_path}"], "skipped": True}
    model = model or os.environ.get("SONGSCORE_JUDGE_MODEL", "gemini-2.5-flash")

    contents = [{"role": "user", "parts": [{"text": RUBRIC}]}]
    if refs_dir:
        contents += load_fewshot_refs(refs_dir)
    mime = "audio/mp3" if audio.suffix.lower() == ".mp3" else "audio/wav"
    target_text = "Rate this song." + (f" Context: {context}" if context else "")
    contents.append({"role": "user", "parts": [
        {"inline_data": {"mime_type": mime,
                         "data": base64.b64encode(audio.read_bytes()).decode()}},
        {"text": target_text}]})

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    req = urllib.request.Request(url, data=json.dumps({"contents": contents}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            payload = json.loads(resp.read().decode())
    except Exception as exc:
        return {"score": 0.0, "issues": [f"Judge call failed: {exc}"], "skipped": True}

    try:
        text = payload["candidates"][0]["content"]["parts"][0]["text"]
        sub = _extract_json(text)
        dims = ["musicality", "vocal_quality", "arrangement", "emotional_impact"]
        vals = [float(sub[d]) for d in dims]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        return {"score": 0.0, "issues": [f"Judge response unparseable: {exc}"], "skipped": True}

    return {"score": round(sum(vals) / len(vals), 2),
            "sub_scores": {d: sub[d] for d in dims},
            "summary": sub.get("summary", ""), "issues": []}
