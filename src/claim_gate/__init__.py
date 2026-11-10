"""Hermes entry point for ClaimGate.

Slice 2 composition root: it registers the model-facing submission tool, the
read-only operator/diagnostic tools, and the fail-closed interception hook.
Composition only -- no business rule lives in this module.
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
    """Register the ClaimGate tools and the interception hook."""

    from .hooks import execution_gate
    from .tools import admin, submissions

    ctx.register_tool(
        name="submit_claim_package",
        toolset="claim_gate",
        schema=submissions.SUBMIT_CLAIM_PACKAGE_SCHEMA,
        handler=submissions.submit_claim_package,
    )

    # Read-only tools. They never write to the trusted registries and they
    # never release a claim for human review.
    ctx.register_tool(
        name="claim_check",
        toolset="claim_gate",
        schema=admin.CLAIM_CHECK_SCHEMA,
        handler=admin.claim_check,
    )
    ctx.register_tool(
        name="claim_policy_get",
        toolset="claim_gate",
        schema=admin.CLAIM_POLICY_GET_SCHEMA,
        handler=admin.claim_policy_get,
    )
    ctx.register_tool(
        name="claim_audit",
        toolset="claim_gate",
        schema=admin.CLAIM_AUDIT_SCHEMA,
        handler=admin.claim_audit,
    )

    ctx.register_hook("pre_tool_call", execution_gate.pre_tool_call)
    logger.info("ClaimGate %s registered the Slice 2 evidence firewall", PLUGIN_VERSION)
