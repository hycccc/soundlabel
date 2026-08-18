"""Generation backends. Core ships exactly one: the free mock synthesizer.

Commercial adapters are deliberately not bundled — implement
:class:`~soundlabel.backends.base.GenerationBackend` and register it here.
"""

from __future__ import annotations

from .base import GenerationBackend, GenerationResult
from .mock import MockBackend

_REGISTRY: dict[str, type[GenerationBackend]] = {
    "mock": MockBackend,
}


def register(name: str, backend_cls: type[GenerationBackend]) -> None:
    _REGISTRY[name] = backend_cls


def get_backend(name: str) -> GenerationBackend:
    try:
        return _REGISTRY[name]()
    except KeyError:
        known = ", ".join(sorted(_REGISTRY))
        raise KeyError(f"unknown backend {name!r}; registered: {known}") from None


__all__ = ["GenerationBackend", "GenerationResult", "MockBackend", "get_backend", "register"]
