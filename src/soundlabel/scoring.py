"""The scoring stack: rules gate, then rank, then (optionally) a judge.

Tiering philosophy — *auto metrics as guardrails, human eval as gold*:

- The **gate** is cheap and binary: clipping, silence, broken duration. It
  exists to stop garbage before anyone (human or model) spends time on it.
- The **rank** is a 0-10 heuristic used to order candidates, not to crown
  them. If `songscore <https://github.com/hycccc/songscore>`_ is installed
  its calibrated multi-dimension stack is used; otherwise a built-in lite
  scorer covers the same ground with coarser features.
- Human listening stays the gold standard; nothing in this module pretends
  otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import soundfile as sf

GATE_MAX_CLIP_RATE = 0.001     # fraction of samples at full scale
GATE_MIN_DURATION_S = 5.0
GATE_MAX_DURATION_S = 600.0
GATE_MIN_RMS_DB = -40.0        # quieter than this is effectively silence


@dataclass
class ScoreReport:
    gate_passed: bool
    gate_reasons: list[str]
    rank_score: float          # 0-10
    scorer: str                # "songscore" | "lite"
    detail: dict = field(default_factory=dict)


def _load_mono(path: str | Path) -> tuple[np.ndarray, int]:
    audio, sr = sf.read(str(path), always_2d=True)
    return audio.mean(axis=1), sr


def gate(path: str | Path) -> tuple[bool, list[str]]:
    """Binary acceptance gate. Fast, deterministic, no model involved."""
    mono, sr = _load_mono(path)
    reasons = []
    duration = len(mono) / sr
    if duration < GATE_MIN_DURATION_S:
        reasons.append(f"too short: {duration:.1f}s < {GATE_MIN_DURATION_S}s")
    if duration > GATE_MAX_DURATION_S:
        reasons.append(f"too long: {duration:.1f}s > {GATE_MAX_DURATION_S}s")
    clip_rate = float(np.mean(np.abs(mono) > 0.999))
    if clip_rate > GATE_MAX_CLIP_RATE:
        reasons.append(f"clipping: {clip_rate:.2%} of samples at full scale")
    rms_db = 20 * np.log10(max(float(np.sqrt(np.mean(mono ** 2))), 1e-12))
    if rms_db < GATE_MIN_RMS_DB:
        reasons.append(f"near-silent: RMS {rms_db:.1f} dB < {GATE_MIN_RMS_DB} dB")
    return (not reasons), reasons


def _lite_rank(path: str | Path) -> tuple[float, dict]:
    """Coarse 0-10 rank from signal statistics — order candidates, no more."""
    audio, sr = sf.read(str(path), always_2d=True)
    mono = audio.mean(axis=1)

    # loudness sanity: reward RMS in a mix-typical window
    rms_db = 20 * np.log10(max(float(np.sqrt(np.mean(mono ** 2))), 1e-12))
    loudness = float(np.clip(1 - abs(rms_db + 16) / 20, 0, 1))

    # spectral balance: centroid in a musical band, not hiss or mud
    spec = np.abs(np.fft.rfft(mono[: sr * 30] * np.hanning(len(mono[: sr * 30]))))
    freqs = np.fft.rfftfreq(len(mono[: sr * 30]), 1 / sr)
    centroid = float(np.sum(freqs * spec) / max(np.sum(spec), 1e-12))
    balance = float(np.clip(1 - abs(np.log2(max(centroid, 50) / 1500)) / 3, 0, 1))

    # dynamics: crest factor, flat-lined masters score low
    peak = float(np.max(np.abs(mono)))
    crest_db = 20 * np.log10(max(peak, 1e-12)) - rms_db
    dynamics = float(np.clip((crest_db - 3) / 12, 0, 1))

    # stereo image (mono files get the midpoint, not a penalty of zero)
    if audio.shape[1] >= 2:
        mid = (audio[:, 0] + audio[:, 1]) / 2
        side = (audio[:, 0] - audio[:, 1]) / 2
        ratio = float(np.sqrt(np.mean(side ** 2)) / max(np.sqrt(np.mean(mid ** 2)), 1e-12))
        width = float(np.clip(ratio / 0.5, 0, 1))
    else:
        width = 0.5

    score = 10 * (0.3 * loudness + 0.3 * balance + 0.25 * dynamics + 0.15 * width)
    return round(score, 2), {
        "rms_db": round(rms_db, 1), "centroid_hz": round(centroid),
        "crest_db": round(crest_db, 1), "stereo_width": round(width, 2),
        "components": {"loudness": loudness, "balance": balance,
                       "dynamics": dynamics, "width": width},
    }


def score(path: str | Path, genre: str = "pop") -> ScoreReport:
    """Run gate then rank. Gate failure short-circuits with rank 0."""
    passed, reasons = gate(path)
    if not passed:
        return ScoreReport(False, reasons, 0.0, "gate", {})
    try:
        from songscore.composite import score_song  # type: ignore
        result = score_song(str(path), genre=genre)
        return ScoreReport(True, [], float(result["composite"]), "songscore", result)
    except ImportError:
        rank, detail = _lite_rank(path)
        return ScoreReport(True, [], rank, "lite", detail)
