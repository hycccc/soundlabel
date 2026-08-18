"""The A&R agent: decides what the artist should record next.

The heuristic version reads exactly what a human A&R would read — the
artist's sonic profile and their catalog history — and applies two rules
learned from running a label:

1. **Anti-rut**: never repeat the artist's most-recorded style when it
   already dominates the catalog; listeners tire before the artist does.
2. **Respect the reject**: if the last verdict was a redo/kill, move away
   from that direction instead of retrying it harder.
"""

from __future__ import annotations

import hashlib

from ..brief import Brief
from ..catalog import Artist

STYLE_POOL = ["pop", "ballad", "rnb", "electronic", "folk", "rock"]
MOODS = ["warm", "wistful", "defiant", "playful", "late-night"]
THEMES = [
    "city lights after the last train",
    "an apology that arrived too late",
    "the first day the heat breaks",
    "calling home from somewhere new",
    "a promise kept quietly",
]
KEYS = ["C", "D", "E", "F", "G", "A"]
BPM_BY_STYLE = {"pop": 104, "ballad": 72, "rnb": 88, "electronic": 122, "folk": 92, "rock": 132}


class ANRAgent:
    def write_brief(self, artist: Artist, history: dict, seed: int = 0) -> Brief:
        styles = [s for s in STYLE_POOL if s in artist.sonic_profile.lower()] or STYLE_POOL[:3]

        counts = history.get("style_counts", {})
        total = sum(counts.values())
        if total >= 3:
            dominant = max(counts, key=counts.get)
            if counts[dominant] / total > 0.5 and dominant in styles and len(styles) > 1:
                styles = [s for s in styles if s != dominant]

        recent = history.get("recent_verdicts", [])
        # deterministic pick, varied by artist + catalog size + seed
        h = int(hashlib.sha256(f"{artist.slug}:{total}:{seed}".encode()).hexdigest(), 16)
        if recent and recent[-1] != "accept" and len(styles) > 1:
            styles = styles[1:] + styles[:1]

        style = styles[h % len(styles)]
        mode = "minor" if history.get("n_tracks", 0) % 3 == 2 else "major"
        return Brief(
            artist_slug=artist.slug,
            style_tags=[style],
            mood=MOODS[h % len(MOODS)],
            theme=THEMES[h % len(THEMES)],
            bpm=float(BPM_BY_STYLE[style]),
            key=KEYS[h % len(KEYS)],
            mode=mode,
            bars=8,
            seed=h % 10_000,
        )
