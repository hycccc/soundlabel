"""Self-contained audio feature extraction for the local scoring dimensions.

Everything is computed directly from the audio file with numpy/scipy — no
external analysis pipeline required. Estimates are guardrail-grade by design:
good enough to gate and rank at scale, never a substitute for ears.
"""

from __future__ import annotations

import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfilt, welch

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

KRUMHANSL = {
    "major": [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88],
    "minor": [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17],
}


def _db(x: float) -> float:
    return float(20 * np.log10(max(x, 1e-12)))


def _band_pct(f: np.ndarray, p: np.ndarray, lo: float, hi: float) -> float:
    total = p.sum() or 1e-20
    return float(p[(f >= lo) & (f < hi)].sum() / total * 100)


def _onset_envelope(mono: np.ndarray, sr: int, hop: int = 512):
    frames = len(mono) // hop
    energy = np.array([np.sum(mono[i * hop:(i + 1) * hop] ** 2) for i in range(frames)])
    onset = np.maximum(0, np.diff(energy, prepend=energy[0]))
    return onset, sr / hop


def estimate_tempo(mono: np.ndarray, sr: int) -> tuple[float, float]:
    """Autocorrelation of the onset envelope → (bpm, confidence 0-1)."""
    onset, fps = _onset_envelope(mono, sr)
    if onset.sum() == 0:
        return 0.0, 0.0
    scores = {}
    for bpm in np.arange(60, 181, 0.5):
        lag = int(round(fps * 60 / bpm))
        if lag < 1 or lag >= len(onset):
            continue
        scores[bpm] = float(np.dot(onset[:-lag], onset[lag:]))
    if not scores:
        return 0.0, 0.0
    best_bpm = max(scores, key=scores.get)
    vals = np.array(list(scores.values()))
    spread = vals.max() - np.median(vals)
    confidence = float(np.clip(spread / (vals.max() - vals.min() + 1e-12), 0, 1))
    return float(best_bpm), round(confidence, 4)


def beat_regularity_std(mono: np.ndarray, sr: int) -> float:
    """Std of inter-onset intervals (seconds) between prominent onsets."""
    onset, fps = _onset_envelope(mono, sr)
    if onset.max() == 0:
        return 0.15
    threshold = onset.mean() + 1.5 * onset.std()
    peaks = []
    refractory = int(fps * 0.2)
    last = -refractory
    for i in range(1, len(onset) - 1):
        if onset[i] > threshold and onset[i] >= onset[i - 1] and onset[i] >= onset[i + 1]:
            if i - last >= refractory:
                peaks.append(i)
                last = i
    if len(peaks) < 4:
        return 0.15
    intervals = np.diff(peaks) / fps
    return float(np.std(intervals))


def chroma_and_key(mono: np.ndarray, sr: int) -> tuple[dict, str, float]:
    """Chromagram (12 pitch classes) + best key + Krumhansl correlation confidence."""
    n = 8192
    chroma = np.zeros(12)
    for start in range(0, len(mono) - n, n):
        frame = mono[start:start + n] * np.hanning(n)
        mag = np.abs(np.fft.rfft(frame))
        freqs = np.fft.rfftfreq(n, 1 / sr)
        mask = (freqs >= 80) & (freqs <= 2000)
        midi = np.round(12 * np.log2(np.maximum(freqs[mask], 1) / 440) + 69).astype(int) % 12
        for pc, m in zip(midi, np.log1p(mag[mask])):
            chroma[pc] += m
    chroma_map = {NOTE_NAMES[i]: round(float(chroma[i]), 3) for i in range(12)}
    if chroma.sum() == 0:
        return chroma_map, "unknown", 0.0
    best, best_r = ("C", "major"), -1.0
    normalized = (chroma - chroma.mean()) / (chroma.std() + 1e-12)
    for mode, profile in KRUMHANSL.items():
        prof = np.array(profile)
        prof = (prof - prof.mean()) / prof.std()
        for tonic in range(12):
            r = float(np.dot(np.roll(normalized, -tonic), prof) / 12)
            if r > best_r:
                best_r, best = r, (NOTE_NAMES[tonic], mode)
    return chroma_map, f"{best[0]} {best[1]}", round(max(0.0, best_r), 4)


def extract_features(path: str) -> dict:
    """Compute the full feature dict the scoring dimensions consume."""
    data, sr = sf.read(path, always_2d=True)
    mono = data.mean(axis=1)
    duration = len(mono) / sr

    peak = float(np.abs(data).max())
    rms = float(np.sqrt(np.mean(mono ** 2)))

    # loudness proxy: K-weighting approximation (2nd-order 60 Hz HPF + shelf)
    sos = butter(2, 60, "high", fs=sr, output="sos")
    kw = sosfilt(sos, mono)
    lufs = float(-0.691 + 10 * np.log10(np.mean(kw ** 2) + 1e-12))

    f, psd = welch(mono, fs=sr, nperseg=min(8192, len(mono)))
    total = psd.sum() or 1e-20
    centroid = float((f * psd).sum() / total)

    if data.shape[1] >= 2:
        mid = (data[:, 0] + data[:, 1]) / 2
        side = (data[:, 0] - data[:, 1]) / 2
        width = float(np.sqrt(np.mean(side ** 2)) / (np.sqrt(np.mean(mid ** 2)) + 1e-12) * 100)
    else:
        width = 0.0

    bpm, bpm_conf = estimate_tempo(mono, sr)
    chroma_map, key, key_conf = chroma_and_key(mono, sr)

    return {
        "duration_s": round(duration, 2),
        "sample_rate": sr,
        "lufs": round(lufs, 2),
        "peak_dbfs": round(_db(peak), 2),
        "crest_db": round(_db(peak) - _db(rms), 2),
        "stereo_width_pct": round(width, 2),
        "low_pct": round(_band_pct(f, psd, 20, 250), 2),
        "mid_pct": round(_band_pct(f, psd, 250, 4000), 2),
        "high_pct": round(_band_pct(f, psd, 4000, 20000), 2),
        "centroid_hz": round(centroid, 1),
        "bpm": bpm,
        "bpm_confidence": bpm_conf,
        "key": key,
        "key_confidence": key_conf,
        "beat_regularity": round(beat_regularity_std(mono, sr), 4),
        "chromagram": chroma_map,
    }
