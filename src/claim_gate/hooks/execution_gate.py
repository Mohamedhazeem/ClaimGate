"""Slice 2 interception gate: strict normalization, trusted registry, policy.

This module is the enforcement point for ``submit_claim_package``.

Contract:

* Every denial on the protected path returns exactly
  ``{"action": "block", "message": "..."}``. A block is never expressed as
  ``{"error": ...}``: Hermes only intercepts a tool call when the hook
  returns an explicit block directive, so any other shape risks letting the
  protected handler run.
* Every ordinary exception on the protected path is converted into a block.
  The hook never raises into the agent loop and never falls through to an
  implicit allow.
* Only ``submit_claim_package`` is protected. Any other tool returns
  ``None`` and is not touched.

The policy rules themselves live in ``claim_gate.domain.policy``; this hook
only resolves trusted records, calls the engine, and translates the decision
into a Hermes directive.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from claim_gate.domain.claim import ValidationError, normalize_claim
from claim_gate.domain.policy import Decision, load_policy
from claim_gate.services.engine import DecisionEngine
from claim_gate.services.registry import RegistryViolation, resolve_evidence

__all__ = ["PROTECTED_TOOL", "post_tool_call", "pre_tool_call"]

logger = logging.getLogger(__name__)

PROTECTED_TOOL = "submit_claim_package"

_FAIL_CLOSED = "ClaimGate: internal verification error; nothing was submitted"


def _block(message: str) -> dict[str, str]:
    """Build the only directive ClaimGate may return on the protected path."""

    return {"action": "block", "message": message}


def pre_tool_call(
    tool_name: str,
    args: Any = None,
    task_id: str = "",
    **kwargs: Any,
) -> dict[str, str] | None:
    """Block the protected tool unless the package passes policy evaluation.

    Returns ``None`` only for a deterministic PASS. Every other outcome --
    malformed arguments, a bad date, a forged/cross-claim/unverified ID, a
    policy problem, a missing document, a review trigger, a waiting period,
    or any unexpected error -- returns an explicit block directive.
    """

    if tool_name != PROTECTED_TOOL:
        return None

    if not isinstance(args, Mapping):
        return _block("ClaimGate: Invalid arguments")

    try:
        claim = normalize_claim(args)
        policy = load_policy()
        evidence, facts = resolve_evidence(
            claim.claim_id,
            claim.evidence_ids,
            claim.incident_fact_ids,
        )
    except RegistryViolation as exc:
        # Forged, cross-claim, or unverified registry reference.
        logger.warning("ClaimGate registry violation: %s (%s)", exc.code, exc.record_id)
        return _block(f"ClaimGate: {exc}")
    except ValidationError as exc:
        # Malformed claim arguments, including a non ISO-8601 incident date.
        return _block(f"ClaimGate: {exc}")
    except Exception:
        # A policy, database, or filesystem outage is a block, not a bypass.
        logger.exception("ClaimGate failed closed before policy evaluation")
        return _block(_FAIL_CLOSED)

    try:
        result = DecisionEngine().evaluate(claim, policy, evidence, facts)
    except Exception:
        logger.exception("ClaimGate decision engine failed closed")
        return _block(_FAIL_CLOSED)

    if result.decision is Decision.PASS:
        return None

    # BLOCK and HUMAN_REVIEW both stop the AI here. Slice 3 differentiates
    # them in the audit log; the agent may never distinguish them.
    return _block(result.message)


def post_tool_call(*args: Any, **kwargs: Any) -> None:
    """Observer hook. It never vetoes and never raises into the agent loop."""

    return None
