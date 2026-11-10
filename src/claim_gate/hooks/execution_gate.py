"""Hermes hook adapters for ClaimGate.

Only ``pre_tool_call`` can veto execution. Its contract is fail-closed: every
unexpected condition must produce ``{"action": "block", ...}`` for the
protected submission tool, never a silent allow.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Mapping

from ..domain.claim import normalize_claim
from ..domain.policy import load_policy
from ..services.audit import AuditRepository
from ..services.engine import Decision, Evaluation, assess_claim
from ..services.registry import EvidenceRegistry, RegistryViolation

__all__ = ["pre_tool_call", "post_tool_call"]

logger = logging.getLogger(__name__)

PROTECTED_TOOL = "submit_claim_package"


def _tool_call_id(kwargs: Mapping[str, Any], *, task_id: str, claim_id: str) -> str:
    value = kwargs.get("tool_call_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    # Hermes supplies tool_call_id on supported versions. The fallback keeps
    # audit writes possible for older adapters without weakening the gate.
    return f"adhoc:{task_id or 'unknown'}:{claim_id}:{uuid.uuid4().hex}"


def _block(code: str, message: str, *, package_hash: str | None = None) -> dict[str, str]:
    """Build the only directive ClaimGate is allowed to return on failure."""

    suffix = f" package_hash={package_hash}" if package_hash else ""
    return {
        "action": "block",
        "message": f"ClaimGate {code}: {message}{suffix}",
    }


def _best_effort_block_audit(
    tool_call_id: str,
    claim_id: str,
    *,
    package_hash: str | None = None,
) -> None:
    try:
        AuditRepository().record_audit(
            tool_call_id=tool_call_id,
            claim_id=claim_id or "UNKNOWN",
            decision="BLOCK",
            package_hash=package_hash,
        )
    except Exception:
        # The tool is already being blocked; an audit outage must not turn a
        # block into an allow. Log it for the operator instead.
        logger.exception("ClaimGate could not write the block audit record")


def _guard_submission(
    args: object,
    *,
    task_id: str,
    **kwargs: Any,
) -> dict[str, str] | None:
    claim = normalize_claim(args)
    policy = load_policy()
    evaluation: Evaluation = assess_claim(claim, policy, EvidenceRegistry())
    repository = AuditRepository()
    tool_call_id = _tool_call_id(kwargs, task_id=task_id, claim_id=claim.claim_id)

    decision = evaluation.result.decision
    if decision is Decision.HUMAN_REVIEW and repository.has_release(
        claim.claim_id, evaluation.package_hash
    ):
        # An operator release is valid only for this exact immutable package.
        repository.record_audit(
            tool_call_id=tool_call_id,
            claim_id=claim.claim_id,
            decision="PASS",
            package_hash=evaluation.package_hash,
        )
        logger.info(
            "ClaimGate operator release applied for claim=%s package=%s",
            claim.claim_id,
            evaluation.package_hash,
        )
        return None

    if decision is Decision.BLOCK:
        _best_effort_block_audit(
            tool_call_id,
            claim.claim_id,
            package_hash=evaluation.package_hash,
        )
        return _block(
            evaluation.result.code,
            evaluation.result.message,
            package_hash=evaluation.package_hash,
        )

    if decision is Decision.HUMAN_REVIEW:
        repository.queue_human_review(claim.claim_id, evaluation.package_hash)
        repository.record_audit(
            tool_call_id=tool_call_id,
            claim_id=claim.claim_id,
            decision="HUMAN_REVIEW",
            package_hash=evaluation.package_hash,
        )
        return _block(
            "HUMAN_REVIEW",
            (
                "package queued for operator review; the AI cannot approve it. "
                f"Release with: hermes claimgate review-release {claim.claim_id} "
                f"--package-hash {evaluation.package_hash}"
            ),
            package_hash=evaluation.package_hash,
        )

    # PASS: audit must succeed before execution is allowed.
    repository.record_audit(
        tool_call_id=tool_call_id,
        claim_id=claim.claim_id,
        decision="PASS",
        package_hash=evaluation.package_hash,
    )
    return None


def pre_tool_call(
    tool_name: str,
    args: object = None,
    task_id: str = "",
    **kwargs: Any,
) -> dict[str, str] | None:
    """Gate ``submit_claim_package`` before its handler can run.

    Non-ClaimGate tools are unaffected; returning ``None`` is the only
    non-block path. Every failure on the protected path is converted into an
    explicit Hermes block directive.
    """

    if tool_name != PROTECTED_TOOL:
        return None

    claim_id = ""
    if isinstance(args, Mapping) and isinstance(args.get("claim_id"), str):
        claim_id = args["claim_id"]

    try:
        return _guard_submission(args, task_id=task_id, **kwargs)
    except RegistryViolation as exc:
        logger.warning("ClaimGate registry violation: %s (%s)", exc.code, exc.record_id)
        _best_effort_block_audit(
            _tool_call_id(kwargs, task_id=task_id, claim_id=claim_id),
            claim_id,
        )
        return _block(exc.code, str(exc))
    except Exception:
        # REQUIRED: catch every ordinary failure and fail closed. Do not add a
        # fall-through allow here; a broken gate must never become an open one.
        logger.exception("ClaimGate pre_tool_call failed closed")
        _best_effort_block_audit(
            _tool_call_id(kwargs, task_id=task_id, claim_id=claim_id),
            claim_id,
        )
        return _block(
            "FAIL_CLOSED",
            "internal verification error; nothing was submitted",
        )


def post_tool_call(
    tool_name: str,
    args: object = None,
    result: object = "",
    task_id: str = "",
    duration_ms: int = 0,
    **kwargs: Any,
) -> None:
    """Record the final submission outcome without changing the gate result."""

    if tool_name != PROTECTED_TOOL:
        return None

    try:
        if not isinstance(args, Mapping):
            return None
        claim_id = args.get("claim_id")
        if not isinstance(claim_id, str) or not claim_id.strip():
            return None

        parsed: object = result
        if isinstance(result, str):
            try:
                parsed = json.loads(result)
            except json.JSONDecodeError:
                # A Hermes block message is not JSON. The pre-call audit row
                # already records that decision; nothing to update here.
                return None
        if not isinstance(parsed, Mapping):
            return None

        succeeded = parsed.get("ok") is True
        raw_hash = parsed.get("package_hash")
        package_hash = raw_hash if isinstance(raw_hash, str) and len(raw_hash) == 64 else None
        AuditRepository().record_outcome(
            tool_call_id=_tool_call_id(
                kwargs, task_id=task_id, claim_id=claim_id.strip()
            ),
            claim_id=claim_id.strip(),
            succeeded=succeeded,
            package_hash=package_hash,
        )
    except Exception:
        # Observer hooks must not break the session. The enforcement decision
        # was already made and audited (or blocked) by pre_tool_call.
        logger.exception("ClaimGate post_tool_call audit failed")
    return None
