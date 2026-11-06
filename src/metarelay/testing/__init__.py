"""Test helpers for MetaRelay and for anyone building on it.

- ``MemorySession`` / ``MemoryBackend``: an in-memory fake backend.
- ``BackendConformance``: the conformance suite every backend must pass.
  Needs pytest: ``pip install 'metarelay[testing]'``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from metarelay.testing.memory import MemoryBackend, MemorySession

if TYPE_CHECKING:
    from metarelay.testing.conformance import BackendConformance

__all__ = ["BackendConformance", "MemoryBackend", "MemorySession"]


def __getattr__(name: str) -> Any:
    # Imported lazily so the fake works without pytest installed.
    if name == "BackendConformance":
        try:
            from metarelay.testing.conformance import BackendConformance
        except ImportError as error:
            raise ImportError(
                "The conformance suite needs pytest. Install it with: "
                "pip install 'metarelay[testing]'"
            ) from error
        return BackendConformance
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
