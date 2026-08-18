"""The generation backend contract.

Two rules every backend must honor:

1. ``cost_estimate`` is consulted *before* ``generate`` is ever called. A
   backend that spends money (API credits, GPU time billed by the minute)
   must return a positive estimate; the pipeline refuses to call it unless
   the operator explicitly allowed paid generation for this run. "Nothing
   spends money on a timer by default" lives here.
2. ``generate`` returns a local audio file. Remote backends download their
   result; the rest of the framework never touches vendor URLs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from ..brief import Brief


@dataclass
class GenerationResult:
    audio_path: Path
    backend: str
    # backend-private parameters; the pipeline stores these in the manifest
    # but never forwards them to the Critic
    params: dict = field(default_factory=dict)
    cost: float = 0.0


class GenerationBackend(ABC):
    name: str = "abstract"

    @abstractmethod
    def cost_estimate(self, brief: Brief) -> float:
        """Estimated cost of one generation, in the backend's own currency.

        Return 0.0 only if generation is genuinely free.
        """

    @abstractmethod
    def generate(self, brief: Brief, out_dir: Path) -> GenerationResult:
        """Produce one track for the brief; write audio under ``out_dir``."""
