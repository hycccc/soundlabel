import numpy as np
import soundfile as sf

from soundlabel.scoring import gate
from soundlabel.scoring.composite import score_song
from soundlabel.scoring.features import extract_features
from soundlabel.scoring.dimensions import score_lyrics_fit, score_melody_proxy, score_originality

SR = 44100


def _tonal_track(tmp_path, name="song.wav", bpm=120, seconds=8.0):
    """Stereo C-major chord loop with kick — tonal, rhythmic, wide."""
    t = np.arange(int(SR * seconds)) / SR
    chord = sum(0.12 * np.sin(2 * np.pi * f * t) * np.exp(-1.5 * (t % (60 / bpm)))
                for f in (261.63, 329.63, 392.0, 523.25))
    beat = 60 / bpm
    kick = np.zeros_like(t)
    for b in np.arange(0, seconds, beat):
        idx = int(b * SR)
        seg = np.arange(min(int(0.1 * SR), len(t) - idx)) / SR
        kick[idx:idx + len(seg)] += 0.4 * np.sin(2 * np.pi * (100 * np.exp(-20 * seg) + 50) * seg) * np.exp(-15 * seg)
    left = chord + kick
    right = 0.9 * chord + kick
    path = tmp_path / name
    sf.write(path, np.stack([left, right], axis=1) * 0.7, SR)
    return str(path)


def test_features_detect_tempo_and_key(tmp_path):
    f = extract_features(_tonal_track(tmp_path))
    assert abs(f["bpm"] - 120) <= 3 or abs(f["bpm"] - 60) <= 2  # octave ambiguity allowed
    assert f["key"].startswith(("C", "A"))  # relative pair acceptable for chords-only content
    assert f["duration_s"] > 7


def test_melody_proxy_prefers_tonal_over_noise(tmp_path):
    tonal = extract_features(_tonal_track(tmp_path))
    noise = np.random.default_rng(0).standard_normal(SR * 4) * 0.1
    sf.write(tmp_path / "noise.wav", noise, SR)
    noisy = extract_features(str(tmp_path / "noise.wav"))
    assert score_melody_proxy(tonal)["score"] > score_melody_proxy(noisy)["score"]


def test_lyrics_fit_scores_structure():
    lyrics = "[Verse]\n" + "\n".join(f"line {i} rhyme" for i in range(8)) + \
             "\n[Chorus]\nhold me close tonight\nhold me close tonight"
    r = score_lyrics_fit(lyrics, duration_s=60)
    assert 0 < r["score"] <= 10
    empty = score_lyrics_fit("", 60)
    assert empty["score"] == 0.0


def test_originality_jaccard():
    same = score_originality("我们一起走过的日子", "我们一起走过的日子")
    diff = score_originality("完全不同的新歌词内容啊", "我们一起走过的日子")
    assert same["score"] < diff["score"]


def test_score_song_end_to_end(tmp_path):
    result = score_song(_tonal_track(tmp_path), lyrics="[Verse]\n你好世界\n[Chorus]\n继续唱歌")
    assert 0 <= result["composite"] <= 10
    assert set(result["dimensions"]) >= {"production", "melody_proxy", "vocal_presence"}
    assert "aesthetic" not in result["weights"]  # judge off by default
