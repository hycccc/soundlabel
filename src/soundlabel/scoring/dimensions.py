"""The five local scoring dimensions (rule-tier of the stack).

Ported from the production pipeline step with thresholds intact. Each scorer
returns {"score": 0-10, "issues": [...], ...diagnostics} — issues are written
for humans, because a score without a reason is not actionable.
"""

from __future__ import annotations

import math
import re

# Genre reference targets, measured from a reference corpus of commercial
# Mandopop-adjacent releases (band energy %, integrated loudness proxy).
GENRE_REFERENCES = {
    "pop":       {"low": 54, "mid": 45, "high": 1,  "lufs": -9.5},
    "ballad":    {"low": 45, "mid": 52, "high": 3,  "lufs": -9.0},
    "edm":       {"low": 85, "mid": 11, "high": 4,  "lufs": -7.6},
    "hip_hop":   {"low": 63, "mid": 35, "high": 2,  "lufs": -9.3},
    "rock":      {"low": 76, "mid": 24, "high": 0,  "lufs": -10.1},
    "rnb":       {"low": 47, "mid": 48, "high": 5,  "lufs": -7.7},
    "jazz":      {"low": 15, "mid": 83, "high": 2,  "lufs": -14.2},
    "classical": {"low": 21, "mid": 78, "high": 1,  "lufs": -12.5},
}


def score_production(features: dict, genre: str = "pop") -> dict:
    """Dimension 1: production quality from signal metrics."""
    issues = []
    ref = GENRE_REFERENCES.get(genre, GENRE_REFERENCES["pop"])
    lufs = features.get("lufs", -99)
    crest = features.get("crest_db", 0)
    peak = features.get("peak_dbfs", 0)
    width = features.get("stereo_width_pct", 0)
    low_pct = features.get("low_pct", 0)
    mid_pct = features.get("mid_pct", 0)
    high_pct = features.get("high_pct", 0)

    score = 10.0
    if lufs < -14:
        score -= min(3.0, (-14 - lufs) * 0.3)
        issues.append(f"LUFS too low ({lufs:.1f}), target range -14 to -8")
    elif lufs > -8:
        score -= min(3.0, (lufs + 8) * 0.5)
        issues.append(f"LUFS too high ({lufs:.1f}), likely over-compressed")

    freq_deviation = (abs(low_pct - ref["low"]) + abs(mid_pct - ref["mid"]) + abs(high_pct - ref["high"])) / 3.0
    if freq_deviation > 20:
        score -= 2.0
        issues.append(f"Frequency balance far from {genre} reference (avg deviation {freq_deviation:.0f}%)")
    elif freq_deviation > 10:
        score -= 1.0
        issues.append(f"Frequency balance somewhat off for {genre} (avg deviation {freq_deviation:.0f}%)")

    if crest < 5:
        score -= 1.5
        issues.append(f"Crest factor too low ({crest:.1f}dB), mix sounds crushed")
    elif crest > 18:
        score -= 1.0
        issues.append(f"Crest factor too high ({crest:.1f}dB), mix may be unbalanced")

    if peak > -0.1:
        score -= 1.5
        issues.append(f"Peak at {peak:.1f} dBFS, likely clipping")

    if width and width < 3:
        score -= 1.0
        issues.append(f"Stereo width very narrow ({width:.1f}%)")
    elif width > 30:
        score -= 0.5
        issues.append(f"Stereo width excessive ({width:.1f}%), may cause phase issues")

    if high_pct < 2:
        score -= 0.5
        issues.append(f"High frequency presence low ({high_pct:.1f}%), mix may sound dull")

    return {"score": round(max(0.0, min(10.0, score)), 2), "issues": issues,
            "freq_deviation": round(freq_deviation, 1), "genre": genre}


def _get_finals(text: str) -> list:
    """Line-ending phonetic finals; pypinyin when available, last char otherwise."""
    lines = [re.sub(r"\[.*?\]", "", l).strip() for l in text.splitlines()]
    lines = [l for l in lines if l]
    try:
        from pypinyin import Style, pinyin
        finals = []
        for line in lines:
            py = pinyin(line[-1], style=Style.FINALS_TONE3, errors="ignore")
            finals.append(re.sub(r"\d", "", py[0][0]) if py and py[0] else line[-1])
        return finals
    except ImportError:
        return [l[-1] for l in lines]


def score_lyrics_fit(lyrics: str, duration_s: float) -> dict:
    """Dimension 2: lyrics density, structure, rhyme, repetition."""
    issues = []
    score = 10.0
    if not lyrics or not lyrics.strip():
        return {"score": 0.0, "issues": ["No lyrics found"]}

    clean_lines, has_verse, has_chorus = [], False, False
    for line in lyrics.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lower = stripped.lower()
        if re.search(r"\[(verse|主歌)", lower):
            has_verse = True
        if re.search(r"\[(chorus|副歌)", lower):
            has_chorus = True
        content = re.sub(r"\[.*?\]", "", stripped).strip()
        if content:
            clean_lines.append(content)
    if not clean_lines:
        return {"score": 0.0, "issues": ["Lyrics contain only markers, no actual text"]}

    density = sum(len(l) for l in clean_lines) / duration_s if duration_s > 0 else 0
    if density < 1:
        score -= 2.0
        issues.append(f"Lyrics too sparse ({density:.1f} chars/sec)")
    elif density > 4:
        score -= 1.5
        issues.append(f"Lyrics too dense ({density:.1f} chars/sec), may not fit timing")

    if not has_verse and not has_chorus:
        score -= 1.5
        issues.append("No verse/chorus structure markers found")
    elif not has_chorus:
        score -= 0.5
        issues.append("Missing chorus marker")
    elif not has_verse:
        score -= 0.5
        issues.append("Missing verse marker")

    finals = _get_finals(lyrics)
    rhyme_ratio = 0.0
    if len(finals) >= 4:
        rhyme = comparisons = 0
        for i in range(0, len(finals) - 1, 2):
            comparisons += 1
            rhyme += finals[i] == finals[i + 1]
        for i in range(len(finals) - 2):
            comparisons += 1
            rhyme += finals[i] == finals[i + 2]
        rhyme_ratio = rhyme / comparisons if comparisons else 0
        if rhyme_ratio < 0.1:
            score -= 1.5
            issues.append(f"Very low rhyme ratio ({rhyme_ratio:.0%})")
        elif rhyme_ratio < 0.2:
            score -= 0.5
            issues.append(f"Low rhyme ratio ({rhyme_ratio:.0%})")
    else:
        score -= 0.5
        issues.append("Too few lines to evaluate rhyme")

    repetition = 1.0 - len(set(clean_lines)) / len(clean_lines)
    if repetition > 0.6:
        score -= 1.5
        issues.append(f"Excessive repetition ({repetition:.0%} of lines are repeated)")
    elif repetition > 0.4:
        score -= 0.5
        issues.append(f"High repetition ({repetition:.0%})")
    elif repetition < 0.05 and len(clean_lines) > 10:
        score -= 0.5
        issues.append("Almost no repetition, unusual for song lyrics")

    return {"score": round(max(0.0, min(10.0, score)), 2), "issues": issues,
            "density": round(density, 2), "rhyme_ratio": round(rhyme_ratio, 3),
            "repetition_ratio": round(repetition, 3), "line_count": len(clean_lines)}


def _ngrams(text: str, n: int = 3) -> set:
    clean = re.sub(r"\s+", "", re.sub(r"\[.*?\]", "", text))
    return {clean[i:i + n] for i in range(len(clean) - n + 1)} if len(clean) >= n else set()


def score_originality(new_lyrics: str, original_lyrics: str) -> dict:
    """Dimension 3: 3-gram Jaccard distance from a source text."""
    a, b = _ngrams(new_lyrics or ""), _ngrams(original_lyrics or "")
    if not a or not b:
        return {"score": 10.0, "similarity_pct": 0.0, "issues": []}
    overlap = len(a & b) / len(a | b)
    similarity_pct = round(overlap * 100, 1)
    issues = []
    if similarity_pct > 60:
        issues.append(f"Very high similarity ({similarity_pct}%) with original, may lack originality")
    elif similarity_pct > 40:
        issues.append(f"Moderate similarity ({similarity_pct}%) with original")
    return {"score": round(max(0.0, min(10.0, 10.0 * (1 - overlap))), 2),
            "similarity_pct": similarity_pct, "issues": issues}


def score_melody_proxy(features: dict) -> dict:
    """Dimension 4: melody-coherence proxy from tonal/rhythmic stability."""
    key_confidence = features.get("key_confidence", 0)
    bpm_confidence = features.get("bpm_confidence", 0)
    beat_reg_std = features.get("beat_regularity", 0.1)
    beat_regularity_score = max(0.0, min(1.0, 1.0 - beat_reg_std / 0.15))

    chromagram = features.get("chromagram", {})
    values = list(chromagram.values())
    total = sum(values)
    if total > 0:
        probs = [v / total for v in values]
        entropy = -sum(p * math.log2(p + 1e-10) for p in probs if p > 0)
        chromagram_focus = max(0.0, min(1.0, 1.0 - entropy / math.log2(12)))
    else:
        chromagram_focus = 0.5

    key_score = 10.0 if key_confidence > 0.7 else key_confidence / 0.7 * 10.0
    beat_score = 10.0 if beat_regularity_score > 0.8 else beat_regularity_score / 0.8 * 10.0
    bpm_score = 10.0 if bpm_confidence > 0.8 else bpm_confidence / 0.8 * 10.0
    composite = (key_score + beat_score + bpm_score + chromagram_focus * 10.0) / 4.0

    issues = []
    if key_confidence < 0.5:
        issues.append(f"Weak tonal center (key confidence {key_confidence:.2f})")
    if beat_regularity_score < 0.5:
        issues.append(f"Irregular beat pattern (std {beat_reg_std:.3f}s)")
    if bpm_confidence < 0.5:
        issues.append(f"Unclear tempo (BPM confidence {bpm_confidence:.2f})")
    if chromagram_focus < 0.2:
        issues.append("Chromagram very spread, weak tonal focus")

    return {"score": round(max(0.0, min(10.0, composite)), 2), "issues": issues,
            "factors": {"key_confidence": round(key_confidence, 4),
                        "beat_regularity": round(beat_regularity_score, 4),
                        "bpm_confidence": round(bpm_confidence, 4),
                        "chromagram_focus": round(chromagram_focus, 4)}}


def score_vocal_presence(features: dict) -> dict:
    """Dimension 5: vocal presence from mid-band energy and centroid."""
    mid_pct = features.get("mid_pct", 0)
    centroid = features.get("centroid_hz", 0)
    score, issues = 10.0, []

    if mid_pct > 40:
        pass
    elif mid_pct > 25:
        score -= 1.5
        issues.append(f"Mid-range energy moderate ({mid_pct:.1f}%), vocals may not stand out")
    else:
        score -= 3.0
        issues.append(f"Mid-range energy low ({mid_pct:.1f}%), vocals likely buried")

    if centroid > 0:
        if 800 <= centroid <= 3000:
            pass
        elif 500 <= centroid < 800:
            score -= 0.5
            issues.append(f"Spectral centroid low ({centroid:.0f}Hz), mix is bass-heavy")
        elif 3000 < centroid <= 5000:
            score -= 0.5
            issues.append(f"Spectral centroid high ({centroid:.0f}Hz), mix may sound thin")
        elif centroid < 500:
            score -= 1.5
            issues.append(f"Spectral centroid very low ({centroid:.0f}Hz), very bass-heavy")
        else:
            score -= 1.0
            issues.append(f"Spectral centroid very high ({centroid:.0f}Hz), harsh or sibilant")

    if mid_pct < 25 and (centroid < 500 or centroid > 5000):
        score -= 1.0
        issues.append("Both mid energy and centroid suggest poor vocal presence")

    return {"score": round(max(0.0, min(10.0, score)), 2), "issues": issues,
            "mid_pct": round(mid_pct, 1), "centroid": round(centroid, 1)}
