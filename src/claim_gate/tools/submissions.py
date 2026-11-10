"""Model-facing claim submission tool for Slice 0.

This is intentionally a dummy handler. It does not touch SQLite or policy
evaluation; its only purpose is to prove that the `pre_tool_call` gate can
allow a call to reach the handler. The global counter is the pass-gate signal.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = [
    "SUBMIT_CLAIM_PACKAGE_SCHEMA",
    "handler_invocation_count",
    "reset_handler_invocation_count",
    "submit_claim_package",
]

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
    """Reset the Slice 0 invocation counter (test helper)."""

    global handler_invocation_count
    handler_invocation_count = 0


def submit_claim_package(args: dict[str, Any], **kwargs: Any) -> str:
    """Dummy handler: record that the gate allowed execution."""

    global handler_invocation_count
    handler_invocation_count += 1
    return json.dumps(
        {
            "ok": True,
            "message": "Claim package submitted.",
            "handler_invocation_count": handler_invocation_count,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
