"""Hermes entry point for ClaimGate.

Slice 3 composition root: it registers the model-facing submission tool, the
read-only operator/diagnostic tools, the fail-closed interception hook, and
the operator-only CLI. Composition only -- no business rule lives in this
module.

The operator CLI is registered through ``register_cli_command`` and is never
exposed as a tool. The agent therefore has no path that approves, releases,
or mutates a review row.
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
    """Register the ClaimGate tools, hooks, and operator CLI."""

    from .cli import review as review_cli
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
    ctx.register_hook("post_tool_call", execution_gate.post_tool_call)

    # Operator-only CLI. It is never registered as a tool, so the model has
    # no way to release its own claim.
    ctx.register_cli_command(
        name="claimgate",
        help="Inspect and release ClaimGate human-review packages",
        description=(
            "Operator commands for ClaimGate's human-review queue and the "
            "append-only claim ledger."
        ),
        setup_fn=review_cli.setup_argparse,
        handler_fn=review_cli.handle_cli,
    )

    logger.info("ClaimGate %s registered the Slice 3 evidence firewall", PLUGIN_VERSION)
