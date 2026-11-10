"""Native Hermes plugin entry point.

Hermes imports this module and calls ``register(ctx)``. ClaimGate uses a
``src/`` layout, so the local source root is added to ``sys.path`` before the
package import. This keeps the installed plugin directory self-contained and
avoids requiring a separate pip install during development.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = str(Path(__file__).resolve().parent / "src")
if _SRC in sys.path:
    sys.path.remove(_SRC)
sys.path.insert(0, _SRC)

from claim_gate import PLUGIN_ID, PLUGIN_ROOT, PLUGIN_VERSION, register  # noqa: E402

__all__ = ["PLUGIN_ID", "PLUGIN_ROOT", "PLUGIN_VERSION", "register"]
