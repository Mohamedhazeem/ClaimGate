"""Type stubs for the Hermes host-provided constants module.

``hermes_constants`` is supplied by the Hermes Agent at runtime. Only the
``get_hermes_home`` helper is stubbed here; ClaimGate does not import it
directly in runtime code.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["get_hermes_home"]

def get_hermes_home() -> Path:
    """Return the active Hermes home directory."""
