"""Type stubs for the Hermes host-provided plugin storage API.

``plugins.plugin_storage`` is supplied by the Hermes Agent at runtime -- it is
not a third-party package and not a declared dependency of this plugin. It is
imported lazily inside ``services/registry.py::connect()`` so that importing
ClaimGate outside a Hermes process does not fail.

These stubs exist only so type checkers and IDEs can resolve the import. They
describe the subset of the host API that ClaimGate uses; they are never
imported at runtime.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

__all__ = ["plugin_data_dir", "plugin_db"]

def plugin_data_dir(name: str) -> Path:
    """Return (and create) ``<hermes home>/plugin-data/<name>/``."""

def plugin_db(name: str, filename: str = "data.db") -> sqlite3.Connection:
    """Open ``<data dir>/`` and return the connection."""
