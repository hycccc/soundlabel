"""The agent loop: A&R writes the brief, the Producer runs the pipeline,
the Critic judges the result blind.

The default agents are deterministic heuristics so the loop runs with zero
API cost; each is a small class you can swap for an LLM-backed version (the
interface is one method each). The structural rule that survives any swap:
the Critic's input type is :class:`~soundlabel.brief.BlindBrief` — it cannot
see generation parameters no matter what intelligence sits behind it.
"""

from .anr import ANRAgent
from .critic import CriticAgent, Verdict

__all__ = ["ANRAgent", "CriticAgent", "Verdict"]
