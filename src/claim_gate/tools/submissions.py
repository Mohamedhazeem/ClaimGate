"""Model-facing claim submission tool.

The schema exposes references only. The handler performs a second,
defense-in-depth evaluation even though ``pre_tool_call`` already gates the
call: a tool invoked through a direct dispatch path must not be able to
bypass the trust boundary.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

from ..domain.claim import normalize_claim
from ..domain.policy import load_policy
from ..services.audit import AuditRepository
from ..services.engine import Decision, assess_claim
from ..services.registry import EvidenceRegistry, RegistryViolation

__all__ = ["SUBMIT_CLAIM_PACKAGE_SCHEMA", "submit_claim_package"]

logger = logging.getLogger(__name__)

SUBMIT_CLAIM_PACKAGE_SCHEMA: dict[str, Any] = {
    "name": "submit_claim_package",
    "description": (
        "Submit an insurance claim package for deterministic evidence-readiness "
        "evaluation. Provide only trusted registry references: evidence_ids and "
        "incident_fact_ids. Never include verification status, document types, "
        "confidence scores, or operator names; ClaimGate resolves and verifies "
        "those server-side. The call is blocked if evidence is forged, missing, "
        "belongs to another claim, is not verified, or violates policy."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "claim_id": {
                "type": "string",
                "description": "Claim identifier, for example CLAIM-101.",
            },
            "policy_id": {
                "type": "string",
                "description": "Policy identifier that must match the active policy.",
            },
            "incident_date": {
                "type": "string",
                "description": (
                    "Incident date in exact YYYY-MM-DD format. Eligibility and "
                    "waiting periods are calculated from this date, not today."
                ),
                "pattern": r"^\d{4}-\d{2}-\d{2}$",
            },
            "claim_type": {
                "type": "string",
                "description": "Policy coverage key, for example collision.",
            },
            "evidence_ids": {
                "type": "array",
                "description": (
                    "Registry IDs for supporting documents, for example EV-001. "
                    "Do not include document statuses or invent IDs."
                ),
                "items": {"type": "string"},
                "maxItems": 100,
            },
            "incident_fact_ids": {
                "type": "array",
                "description": (
                    "Registry IDs for verified incident facts, for example FACT-001. "
                    "Do not include fact values or statuses; the registry supplies them."
                ),
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


def _json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def submit_claim_package(args: dict[str, Any], **kwargs: Any) -> str:
    """Evaluate and, only on PASS, append the claim to the immutable ledger."""

    try:
        claim = normalize_claim(args)
        policy = load_policy()
        evaluation = assess_claim(claim, policy, EvidenceRegistry())
        audit = AuditRepository()

        decision = evaluation.result.decision
        release_applied = False
        if decision is Decision.HUMAN_REVIEW and audit.has_release(
            claim.claim_id, evaluation.package_hash
        ):
            decision = Decision.PASS
            release_applied = True

        common = {
            "claim_id": claim.claim_id,
            "policy_id": policy.policy_id,
            "claim_type": claim.claim_type,
            "package_hash": evaluation.package_hash,
        }

        if decision is Decision.BLOCK:
            return _json(
                {
                    "ok": False,
                    "decision": Decision.BLOCK.value,
                    "code": evaluation.result.code,
                    "message": evaluation.result.message,
                    **common,
                }
            )

        if decision is Decision.HUMAN_REVIEW:
            audit.queue_human_review(claim.claim_id, evaluation.package_hash)
            return _json(
                {
                    "ok": False,
                    "decision": Decision.HUMAN_REVIEW.value,
                    "code": evaluation.result.code,
                    "message": (
                        "ClaimGate queued this package for operator review. "
                        "The AI cannot approve or release it."
                    ),
                    "review_command": (
                        f"hermes claimgate review-release {claim.claim_id} "
                        f"--package-hash {evaluation.package_hash}"
                    ),
                    **common,
                }
            )

        ledger = audit.record_ledger_entry(claim.claim_id, evaluation.package_hash)
        if not ledger.matches:
            return _json(
                {
                    "ok": False,
                    "decision": Decision.BLOCK.value,
                    "code": "CLAIM_HASH_CONFLICT",
                    "message": (
                        "A different package was already recorded for this claim. "
                        "ClaimGate will not overwrite the ledger."
                    ),
                    "existing_package_hash": ledger.existing_hash,
                    **common,
                }
            )

        return _json(
            {
                "ok": True,
                "decision": Decision.PASS.value,
                "code": "OPERATOR_RELEASE" if release_applied else evaluation.result.code,
                "message": "Claim package accepted by ClaimGate.",
                "idempotent": not ledger.inserted,
                "release_applied": release_applied,
                **common,
            }
        )

    except RegistryViolation as exc:
        logger.warning("ClaimGate registry violation: %s (%s)", exc.code, exc.record_id)
        return _json(
            {
                "ok": False,
                "decision": Decision.BLOCK.value,
                "code": exc.code,
                "message": str(exc),
                "record_id": exc.record_id,
            }
        )
    except Exception:
        logger.exception("ClaimGate submission failed closed")
        return _json(
            {
                "ok": False,
                "decision": Decision.BLOCK.value,
                "code": "FAIL_CLOSED",
                "message": (
                    "ClaimGate could not complete trusted verification and failed "
                    "closed. Nothing was submitted."
                ),
            }
        )
