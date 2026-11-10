"""ClaimGate: a deterministic evidence-readiness firewall for Hermes Agent.

The package is intentionally split into domain, services, tools, hooks, and
CLI layers. This module is the composition root: it wires schemas to handlers
and registers the enforcement hooks. It contains no policy logic.
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
    """Register ClaimGate's tools, hooks, and operator CLI with Hermes."""

    from .cli import review
    from .domain.policy import load_policy
    from .hooks import execution_gate
    from .tools import admin, submissions

    # Validate the shipped policy at registration time. A malformed policy
    # must prevent the plugin from loading rather than produce a partial gate.
    policy = load_policy()
    logger.info(
        "ClaimGate %s registering policy=%s hash=%s",
        PLUGIN_VERSION,
        policy.policy_id,
        policy.source_hash,
    )

    ctx.register_tool(
        name="submit_claim_package",
        toolset="claim_gate",
        schema=submissions.SUBMIT_CLAIM_PACKAGE_SCHEMA,
        handler=submissions.submit_claim_package,
    )
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

    ctx.register_cli_command(
        name="claimgate",
        help="Inspect and release ClaimGate human-review packages",
        setup_fn=review.setup_argparse,
        handler_fn=review.handle_command,
        description=(
            "Operator-only ClaimGate controls. The agent has no tool or slash "
            "command capable of approving its own claim."
        ),
    )
