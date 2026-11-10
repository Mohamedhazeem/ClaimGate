"""Slice 3 interception gate: strict normalization, trusted registry, policy,
audit, and the operator release check.

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
* A BLOCK or HUMAN_REVIEW decision is written to ``audit_log`` before the
  block is returned. A PASS is audited *before* the handler is allowed to
  run, so the audit trail records the decision even if the handler later
  fails.
* A HUMAN_REVIEW package that an operator has already released -- for the
  exact package hash -- is allowed to proceed. The release is bound to the
  immutable fingerprint, so any change to the claim, the policy, or the
  resolved registry rows produces a different hash and invalidates it.

The policy rules themselves live in ``claim_gate.domain.policy``; this hook
only resolves trusted records, calls the engine, applies the release check,
and translates the decision into a Hermes directive.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Mapping

from claim_gate.domain.claim import ValidationError, normalize_claim
from claim_gate.domain.policy import Decision, load_policy
from claim_gate.services.audit import AuditRepository, compute_package_hash
from claim_gate.services.engine import DecisionEngine
from claim_gate.services.registry import RegistryViolation, resolve_evidence

__all__ = ["PROTECTED_TOOL", "post_tool_call", "pre_tool_call"]

logger = logging.getLogger(__name__)

PROTECTED_TOOL = "submit_claim_package"

_FAIL_CLOSED = "ClaimGate: internal verification error; nothing was submitted"
_PENDING_REVIEW = "ClaimGate: Pending Human Review"


def _block(message: str) -> dict[str, str]:
    """Build the only directive ClaimGate may return on the protected path."""

    return {"action": "block", "message": message}


def _audit_key(
    kwargs: Mapping[str, Any],
    *,
    task_id: str,
    claim_id: str,
    package_hash: str | None = None,
) -> str:
    """Return the audit key for one tool call.

    Hermes supplies ``tool_call_id`` on supported versions, and both the
    gate and the observer then agree on the key. The fallback is
    deterministic for a given package so the gate and the observer still
    correlate; it never influences a decision.
    """

    value = kwargs.get("tool_call_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    if package_hash:
        return f"adhoc:{task_id or 'unknown'}:{claim_id or 'UNKNOWN'}:{package_hash}"
    return f"adhoc:{task_id or 'unknown'}:{claim_id or 'UNKNOWN'}:{uuid.uuid4().hex}"


def _best_effort_audit(
    tool_call_id: str,
    claim_id: str,
    decision: str,
    package_hash: str | None,
    *,
    hermes_tool_call_id: str | None = None,
) -> None:
    """Write an audit row without ever changing the gate's decision.

    An audit outage must not turn a block into an allow, and it must not
    turn an allow into a crash. The row is best effort; the decision is
    already deterministic.
    """

    try:
        AuditRepository().record_audit(
            tool_call_id=tool_call_id,
            claim_id=claim_id,
            decision=decision,
            package_hash=package_hash,
            hermes_tool_call_id=hermes_tool_call_id,
        )
    except Exception:
        logger.exception("ClaimGate could not write the audit record")


def _ledger_conflict(claim_id: str, package_hash: str) -> str | None:
    """Return a block message when the ledger already holds another package.

    The ledger is append-only and keyed by ``claim_id``. A package whose
    hash differs from the recorded one is a conflict and must never
    overwrite the original row. The same hash is an idempotent replay.
    """

    try:
        recorded = AuditRepository().get_claim(claim_id)
    except Exception:
        logger.exception("ClaimGate could not read the claim ledger")
        return _FAIL_CLOSED

    if recorded is None:
        return None
    if recorded.get("package_hash") == package_hash:
        return None
    return (
        "ClaimGate: claim already submitted with a different package; "
        "the claim ledger is append-only"
    )


def pre_tool_call(
    tool_name: str,
    args: Any = None,
    task_id: str = "",
    **kwargs: Any,
) -> dict[str, str] | None:
    """Block the protected tool unless the package passes policy evaluation.

    Returns ``None`` only for a deterministic PASS, or for a HUMAN_REVIEW
    package that an operator has released for the exact package hash. Every
    other outcome -- malformed arguments, a bad date, a forged/cross-claim/
    unverified ID, a policy problem, a missing document, a review trigger, a
    waiting period, or any unexpected error -- returns an explicit block
    directive.
    """

    if tool_name != PROTECTED_TOOL:
        return None

    if not isinstance(args, Mapping):
        return _block("ClaimGate: Invalid arguments")

    claim_id = ""
    if isinstance(args.get("claim_id"), str):
        claim_id = args["claim_id"]

    hermes_tool_call_id = kwargs.get("tool_call_id")
    if not isinstance(hermes_tool_call_id, str):
        hermes_tool_call_id = None

    def audit(decision: str, package_hash: str | None, claim: str) -> None:
        _best_effort_audit(
            _audit_key(kwargs, task_id=task_id, claim_id=claim, package_hash=package_hash),
            claim,
            decision,
            package_hash,
            hermes_tool_call_id=hermes_tool_call_id,
        )

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
        audit("BLOCK", None, claim_id)
        return _block(f"ClaimGate: {exc}")
    except ValidationError as exc:
        # Malformed claim arguments, including a non ISO-8601 incident date.
        audit("BLOCK", None, claim_id)
        return _block(f"ClaimGate: {exc}")
    except Exception:
        # A policy, database, or filesystem outage is a block, not a bypass.
        logger.exception("ClaimGate failed closed before policy evaluation")
        audit("BLOCK", None, claim_id)
        return _block(_FAIL_CLOSED)

    try:
        result = DecisionEngine().evaluate(claim, policy, evidence, facts)
    except Exception:
        logger.exception("ClaimGate decision engine failed closed")
        audit("BLOCK", None, claim.claim_id)
        return _block(_FAIL_CLOSED)

    try:
        package_hash = compute_package_hash(claim, policy.source_hash, evidence, facts)
    except Exception:
        logger.exception("ClaimGate could not fingerprint the package")
        audit("BLOCK", None, claim.claim_id)
        return _block(_FAIL_CLOSED)

    if result.decision is Decision.BLOCK:
        audit("BLOCK", package_hash, claim.claim_id)
        return _block(result.message)

    if result.decision is Decision.HUMAN_REVIEW:
        # An operator release is valid only for this exact immutable package.
        # Any change to the claim, the policy, or the resolved registry rows
        # changes the hash and therefore invalidates the release.
        try:
            released = AuditRepository().has_release(claim.claim_id, package_hash)
        except Exception:
            logger.exception("ClaimGate could not read the release state")
            return _block(_FAIL_CLOSED)

        if released:
            conflict = _ledger_conflict(claim.claim_id, package_hash)
            if conflict:
                audit("BLOCK", package_hash, claim.claim_id)
                return _block(conflict)
            audit("PASS", package_hash, claim.claim_id)
            logger.info(
                "ClaimGate operator release applied for claim=%s package=%s",
                claim.claim_id,
                package_hash,
            )
            return None

        try:
            AuditRepository().queue_human_review(
                claim.claim_id,
                package_hash,
                reason=result.message,
            )
        except Exception:
            # A queue write failure is not a bypass: the package stays
            # blocked and the operator sees the failure in the logs.
            logger.exception("ClaimGate could not queue the package for review")
            return _block(_FAIL_CLOSED)

        audit("HUMAN_REVIEW", package_hash, claim.claim_id)
        # The AI is told only that a human must act. The queue row and the
        # audit trail keep the specific reason for the operator.
        return _block(_PENDING_REVIEW)

    # The ledger is append-only and keyed by claim_id. A package whose hash
    # differs from the one already on record is a conflict: the original row
    # must never be overwritten. The same hash is an idempotent replay and
    # is allowed through -- the handler reports ALREADY_SUBMITTED.
    conflict = _ledger_conflict(claim.claim_id, package_hash)
    if conflict:
        audit("BLOCK", package_hash, claim.claim_id)
        return _block(conflict)

    # PASS: audit must succeed before execution is allowed.
    try:
        AuditRepository().record_audit(
            tool_call_id=_audit_key(
                kwargs,
                task_id=task_id,
                claim_id=claim.claim_id,
                package_hash=package_hash,
            ),
            claim_id=claim.claim_id,
            decision="PASS",
            package_hash=package_hash,
            hermes_tool_call_id=hermes_tool_call_id,
        )
    except Exception:
        logger.exception("ClaimGate could not write the PASS audit record")
        return _block(_FAIL_CLOSED)

    return None


def post_tool_call(
    tool_name: str = "",
    args: Any = None,
    result: Any = "",
    **kwargs: Any,
) -> None:
    """Record the final submission outcome. It never vetoes and never raises.

    The observer writes ``execution_success`` onto the audit row the gate
    already created. An audit outage here is logged, never propagated: the
    enforcement decision was already made.
    """

    if tool_name != PROTECTED_TOOL:
        return None

    try:
        if not isinstance(args, Mapping):
            return None
        claim_id = args.get("claim_id")
        if not isinstance(claim_id, str) or not claim_id.strip():
            return None

        succeeded = False
        package_hash: str | None = None
        parsed: Any = result
        if isinstance(result, str):
            try:
                parsed = json.loads(result)
            except Exception:
                parsed = None
        if isinstance(parsed, Mapping):
            succeeded = parsed.get("ok") is True
            raw_hash = parsed.get("package_hash")
            if isinstance(raw_hash, str) and len(raw_hash) == 64:
                package_hash = raw_hash

        AuditRepository().record_execution(
            tool_call_id=_audit_key(
                kwargs,
                task_id=str(kwargs.get("task_id", "")),
                claim_id=claim_id.strip(),
                package_hash=package_hash,
            ),
            claim_id=claim_id.strip(),
            succeeded=succeeded,
            package_hash=package_hash,
        )
    except Exception:
        # Observer hooks must not break the session.
        logger.exception("ClaimGate post_tool_call audit failed")
    return None
