"""Hermes entry point for ClaimGate.

Slice 0 composition root: register only the submission tool and the
interception hook. No policy, registry, audit, or CLI wiring is active yet.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

__all__ = ["PLUGIN_ID", "PLUGIN_ROOT", "PLUGIN_VERSION", "register"]

PLUGIN_ID = "claim-gate"
PLUGIN_VERSION = "0.1.0"
PLUGIN_ROOT = Path(__file__).resolve().parents[2]

logger = logging.getLogger(__name__)


def register(ctx: Any) -> None:
    """Register the Slice 0 tool and interception hook."""

    from .hooks import execution_gate
    from .tools import submissions

    ctx.register_tool(
        name="submit_claim_package",
        toolset="claim_gate",
        schema=submissions.SUBMIT_CLAIM_PACKAGE_SCHEMA,
        handler=submissions.submit_claim_package,
    )

    ctx.register_hook("pre_tool_call", execution_gate.pre_tool_call)
    logger.info("ClaimGate %s registered Slice 0 interception gate", PLUGIN_VERSION)
