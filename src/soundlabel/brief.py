"""Production briefs.

Two views of the same brief, and the split is the point:

- ``Brief`` is what the Producer sees — every knob, including generation
  parameters.
- ``BlindBrief`` is what the Critic sees — creative intent only. It is a
  separate frozen type with no reference back to the full brief, so a Critic
  implementation *cannot* condition its verdict on how the track was made.
  Knowledge contamination is prevented structurally, not by convention.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict


@dataclass(frozen=True)
class BlindBrief:
    """The Critic's view: what the track was supposed to be, never how."""

    artist_slug: str
    style_tags: tuple[str, ...]
    mood: str
    theme: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


@dataclass
class Brief:
    """The Producer's view: creative intent plus generation parameters."""

    artist_slug: str
    style_tags: list[str] = field(default_factory=lambda: ["pop"])
    mood: str = "warm"
    theme: str = "untitled"
    title_hint: str = ""
    # generation parameters — hidden from the Critic
    bpm: float = 100.0
    key: str = "C"
    mode: str = "major"
    bars: int = 8
    seed: int = 0

    def blind(self) -> BlindBrief:
        """Strip generation parameters; this is all the Critic ever gets."""
        return BlindBrief(
            artist_slug=self.artist_slug,
            style_tags=tuple(self.style_tags),
            mood=self.mood,
            theme=self.theme,
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, raw: str) -> "Brief":
        return cls(**json.loads(raw))
