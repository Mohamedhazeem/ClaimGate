"""Operator-facing diagnostic tools: dry-run, policy inspection, audit lookup."""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

from ..domain.claim import normalize_claim
from ..domain.policy import load_policy
from ..services.audit import AuditRepository
from ..services.engine import Decision, assess_claim
from ..services.registry import EvidenceRegistry, RegistryViolation

__all__ = [
    "CLAIM_AUDIT_SCHEMA",
    "CLAIM_CHECK_SCHEMA",
    "CLAIM_POLICY_GET_SCHEMA",
    "claim_audit",
    "claim_check",
    "claim_policy_get",
]

logger = logging.getLogger(__name__)

CLAIM_CHECK_SCHEMA: dict[str, Any] = {
    "name": "claim_check",
    "description": (
        "Dry-run ClaimGate's evidence-readiness evaluation without submitting, "
        "queueing review, or writing to the claim ledger. Uses exactly the same "
        "trusted registry and policy path as submit_claim_package. The result is "
        "advisory only; submit_claim_package remains the enforcement point."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "claim_id": {"type": "string"},
            "policy_id": {"type": "string"},
            "incident_date": {
                "type": "string",
                "description": "Incident date in exact YYYY-MM-DD format.",
                "pattern": r"^\d{4}-\d{2}-\d{2}$",
            },
            "claim_type": {"type": "string"},
            "evidence_ids": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 100,
            },
            "incident_fact_ids": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 100,
            },
        },
        "required": [
            "claim_id",
            "policy_id",
            "incident_date",
            "claim_type",
            "evidence_ids",
            "incident_fact_ids",
        ],
        "additionalProperties": False,
    },
}

CLAIM_POLICY_GET_SCHEMA: dict[str, Any] = {
    "name": "claim_policy_get",
    "description": (
        "Return the active ClaimGate policy, its effective term, normalized "
        "coverage rules, and its SHA-256 content hash. Read-only."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
}

CLAIM_AUDIT_SCHEMA: dict[str, Any] = {
    "name": "claim_audit",
    "description": (
        "Read ClaimGate audit history and human-review state for one claim. "
        "Read-only operator visibility; it cannot release a claim."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "claim_id": {
                "type": "string",
                "description": "Claim identifier to inspect.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum audit rows to return (1-200).",
                "minimum": 1,
                "maximum": 200,
                "default": 50,
            },
        },
        "required": ["claim_id"],
        "additionalProperties": False,
    },
}


def _json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _claim_id(args: Mapping[str, Any]) -> str:
    value = args.get("claim_id")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("claim_id must be a non-empty string")
    return value.strip()


def claim_policy_get(args: dict[str, Any], **kwargs: Any) -> str:
    """Return the validated active policy and its content hash."""

    try:
        policy = load_policy()
        return _json(
            {
                "ok": True,
                "policy_id": policy.policy_id,
                "policy_hash": policy.source_hash,
                "policy": policy.to_public_dict(),
            }
        )
    except Exception:
        logger.exception("ClaimGate policy read failed")
        return _json(
            {
                "ok": False,
                "code": "POLICY_UNAVAILABLE",
                "message": "ClaimGate could not load a valid policy and failed closed.",
            }
        )


def claim_check(args: dict[str, Any], **kwargs: Any) -> str:
    """Dry-run the decision pipeline without any side effects."""

    try:
        claim = normalize_claim(args)
        policy = load_policy()
        evaluation = assess_claim(claim, policy, EvidenceRegistry())
        return _json(
            {
                "ok": evaluation.result.decision is Decision.PASS,
                "dry_run": True,
                "decision": evaluation.result.decision.value,
                "code": evaluation.result.code,
                "message": evaluation.result.message,
                "reasons": list(evaluation.result.reasons),
                "claim_id": claim.claim_id,
                "policy_id": policy.policy_id,
                "package_hash": evaluation.package_hash,
                "evidence_ids": [record.evidence_id for record in evaluation.evidence],
                "fact_ids": [record.fact_id for record in evaluation.facts],
            }
        )
    except RegistryViolation as exc:
        return _json(
            {
                "ok": False,
                "dry_run": True,
                "decision": Decision.BLOCK.value,
                "code": exc.code,
                "message": str(exc),
                "record_id": exc.record_id,
            }
        )
    except Exception:
        logger.exception("ClaimGate dry-run failed closed")
        return _json(
            {
                "ok": False,
                "dry_run": True,
                "decision": Decision.BLOCK.value,
                "code": "FAIL_CLOSED",
                "message": (
                    "ClaimGate could not complete trusted verification and failed "
                    "closed. No claim was submitted."
                ),
            }
        )


def claim_audit(args: dict[str, Any], **kwargs: Any) -> str:
    """Return audit and review rows for a claim without mutating state."""

    try:
        claim_id = _claim_id(args)
        limit = args.get("limit", 50)
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("limit must be an integer")
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")

        repository = AuditRepository()
        return _json(
            {
                "ok": True,
                "claim_id": claim_id,
                "audit": repository.get_audit(claim_id, limit=limit),
                "review": repository.get_review(claim_id),
                "ledger": repository.get_claim(claim_id),
            }
        )
    except Exception:
        logger.exception("ClaimGate audit read failed")
        return _json(
            {
                "ok": False,
                "code": "AUDIT_UNAVAILABLE",
                "message": "ClaimGate could not read audit state.",
            }
        )
