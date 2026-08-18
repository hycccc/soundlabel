"""The mock backend: instant, free, fully synthesized audio.

It exists so the whole framework — pipeline, scoring, agents, catalog — can
be exercised end to end without an API key or a copyright question. The
audio is a chord progression with a kick/hat pattern, styled per tag; it is
not meant to sound like a hit, it is meant to be *scoreable*.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

from ..brief import Brief
from .base import GenerationBackend, GenerationResult

SR = 44100
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
MAJOR = [0, 2, 4, 5, 7, 9, 11]
MINOR = [0, 2, 3, 5, 7, 8, 10]
ROMAN = {"i": 0, "ii": 1, "iii": 2, "iv": 3, "v": 4, "vi": 5, "vii": 6}

# style tag -> (progression, drum density multiplier)
STYLE_PROGRESSIONS = {
    "pop": (["I", "V", "vi", "IV"], 1.0),
    "ballad": (["I", "vi", "IV", "V"], 0.5),
    "rnb": (["ii", "V", "I", "vi"], 0.75),
    "electronic": (["vi", "IV", "I", "V"], 1.5),
    "folk": (["I", "IV", "I", "V"], 0.5),
    "rock": (["I", "IV", "V", "IV"], 1.0),
}
DEFAULT_PROGRESSION = STYLE_PROGRESSIONS["pop"]


def _chord_midi(key: str, mode: str, symbol: str) -> list[int]:
    scale = MAJOR if mode == "major" else MINOR
    deg = ROMAN[symbol.lower().rstrip("°")]
    root = 48 + NOTE_NAMES.index(key) + scale[deg]
    third = root + (4 if symbol[0].isupper() else 3)
    return [root, third, root + 7, root + 12]


def _hz(m: int) -> float:
    return 440.0 * 2 ** ((m - 69) / 12)


def _note(freq: float, dur: float, amp: float = 0.15) -> np.ndarray:
    t = np.arange(int(SR * dur)) / SR
    w = sum(g * np.sin(2 * np.pi * freq * k * t) for k, g in [(1, 1), (2, .4), (3, .2), (4, .1)])
    return amp * w * np.exp(-2.0 * t)


def _kick() -> np.ndarray:
    t = np.arange(int(SR * 0.12)) / SR
    f = 110 * np.exp(-18 * t) + 50
    return 0.55 * np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-14 * t)


def _hat(seed: int) -> np.ndarray:
    t = np.arange(int(SR * 0.03)) / SR
    return 0.09 * np.random.default_rng(seed).standard_normal(len(t)) * np.exp(-60 * t)


def render(key: str, mode: str, progression: list[str], bpm: float,
           bars: int = 8, density: float = 1.0, seed: int = 0) -> np.ndarray:
    """Stereo render: chord voices panned, kick centered, hats slightly right."""
    beat = 60 / bpm
    bar = 4 * beat
    n = int(SR * bar * bars)
    L, R = np.zeros(n + SR), np.zeros(n + SR)
    pans = [0.35, 0.65, 0.45, 0.55]
    hats_per_beat = max(1, round(2 * density))
    for b in range(bars):
        start = int(SR * bar * b)
        for vi, m in enumerate(_chord_midi(key, mode, progression[b % len(progression)])):
            v = _note(_hz(m), bar)
            L[start:start + len(v)] += v * (1 - pans[vi])
            R[start:start + len(v)] += v * pans[vi]
        for q in range(4):
            k = _kick()
            at = start + int(SR * beat * q)
            L[at:at + len(k)] += k * 0.5
            R[at:at + len(k)] += k * 0.5
            for h in range(hats_per_beat):
                hh = _hat(seed + b * 16 + q * 4 + h)
                at = start + int(SR * beat * (q + (h + 0.5) / hats_per_beat))
                L[at:at + len(hh)] += hh * 0.4
                R[at:at + len(hh)] += hh * 0.6
    st = np.stack([L[:n], R[:n]], axis=1)
    return 0.7 * st / np.abs(st).max()


class MockBackend(GenerationBackend):
    name = "mock"

    def cost_estimate(self, brief: Brief) -> float:
        return 0.0

    def generate(self, brief: Brief, out_dir: Path) -> GenerationResult:
        style = next((t for t in brief.style_tags if t in STYLE_PROGRESSIONS), "pop")
        progression, density = STYLE_PROGRESSIONS.get(style, DEFAULT_PROGRESSION)
        audio = render(brief.key, brief.mode, progression, brief.bpm,
                       bars=brief.bars, density=density, seed=brief.seed)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{brief.artist_slug}-{style}-{brief.key}{'' if brief.mode == 'major' else 'm'}.wav"
        sf.write(path, audio, SR, subtype="PCM_16")
        return GenerationResult(
            audio_path=path,
            backend=self.name,
            params={"style": style, "progression": progression, "bpm": brief.bpm,
                    "key": brief.key, "mode": brief.mode, "bars": brief.bars, "seed": brief.seed},
            cost=0.0,
        )
