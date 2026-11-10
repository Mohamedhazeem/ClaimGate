"""Model-facing claim submission tool.

The handler is the last step of the protected path. The ``pre_tool_call``
gate has already decided PASS (or observed a valid operator release) before
this runs, so the handler does not repeat the policy evaluation. It does two
things the gate cannot:

1. It appends the accepted package to the append-only ``claim_ledger``,
   keyed by ``claim_id``. A second submission of the same claim never
   creates a second row; it returns ``ALREADY_SUBMITTED``.
2. It reports the final execution outcome, so the audit trail records both
   the decision and what actually happened.

The handler never raises into Hermes: it always returns a JSON string.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..domain.claim import ValidationError, normalize_claim
from ..domain.policy import load_policy
from ..services.audit import AuditRepository, compute_package_hash
from ..services.registry import RegistryViolation, resolve_evidence

__all__ = [
    "SUBMIT_CLAIM_PACKAGE_SCHEMA",
    "handler_invocation_count",
    "reset_handler_invocation_count",
    "submit_claim_package",
]

logger = logging.getLogger(__name__)

SUBMIT_CLAIM_PACKAGE_SCHEMA: dict[str, Any] = {
    "name": "submit_claim_package",
    "description": "Submit a claim package for evidence-readiness evaluation.",
    "parameters": {
        "type": "object",
        "properties": {
            "claim_id": {"type": "string"},
            "policy_id": {"type": "string"},
            "incident_date": {"type": "string"},
            "claim_type": {"type": "string"},
            "evidence_ids": {
                "type": "array",
                "items": {"type": "string"},
            },
            "incident_fact_ids": {
                "type": "array",
                "items": {"type": "string"},
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

handler_invocation_count = 0


def reset_handler_invocation_count() -> None:
    """Reset the invocation counter (test helper)."""

    global handler_invocation_count
    handler_invocation_count = 0


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def submit_claim_package(args: dict[str, Any], **kwargs: Any) -> str:
    """Record an accepted claim package in the append-only ledger."""

    global handler_invocation_count
    handler_invocation_count += 1

    try:
        claim = normalize_claim(args)
        policy = load_policy()
        evidence, facts = resolve_evidence(
            claim.claim_id,
            claim.evidence_ids,
            claim.incident_fact_ids,
        )
        package_hash = compute_package_hash(claim, policy.source_hash, evidence, facts)
    except RegistryViolation as exc:
        logger.warning("ClaimGate submission rejected at the handler: %s", exc.code)
        return _json(
            {
                "ok": False,
                "status": "BLOCKED",
                "code": exc.code,
                "message": str(exc),
            }
        )
    except ValidationError as exc:
        return _json(
            {
                "ok": False,
                "status": "BLOCKED",
                "code": "INVALID_CLAIM",
                "message": str(exc),
            }
        )
    except Exception:
        logger.exception("ClaimGate submission failed")
        return _json(
            {
                "ok": False,
                "status": "FAILED",
                "code": "SUBMISSION_FAILED",
                "message": "ClaimGate could not record the claim package.",
            }
        )

    repository = AuditRepository()
    try:
        ledger = repository.record_ledger_entry(claim.claim_id, package_hash)
    except Exception:
        logger.exception("ClaimGate could not write the claim ledger")
        return _json(
            {
                "ok": False,
                "status": "FAILED",
                "code": "LEDGER_UNAVAILABLE",
                "message": "ClaimGate could not record the claim package.",
            }
        )

    if not ledger.inserted:
        # The ledger is keyed by claim_id and is append-only. The first
        # accepted package for a claim is the one on record.
        return _json(
            {
                "ok": True,
                "status": "ALREADY_SUBMITTED",
                "claim_id": claim.claim_id,
                "package_hash": ledger.existing_hash,
                "matches": ledger.matches,
                "handler_invocation_count": handler_invocation_count,
            }
        )

    return _json(
        {
            "ok": True,
            "status": "SUBMITTED",
            "claim_id": claim.claim_id,
            "package_hash": package_hash,
            "handler_invocation_count": handler_invocation_count,
        }
    )
