"""Slice 1 interception gate: strict date parsing plus trusted registry checks.

Policy evaluation is still Slice 2. This gate allows a submission only when
its date parses and every supplied reference resolves in the operator-controlled
registry.
"""

from __future__ import annotations

from typing import Any

from claim_gate.domain.claim import parse_incident_date
from claim_gate.services.registry import resolve_evidence

__all__ = ["PROTECTED_TOOL", "post_tool_call", "pre_tool_call"]

PROTECTED_TOOL = "submit_claim_package"


def pre_tool_call(tool_name: str, args: Any = None, task_id: str = "", **kwargs: Any) -> dict[str, str] | None:
    """Block the protected tool on bad dates or untrusted IDs."""

    if tool_name != PROTECTED_TOOL:
        return None

    if not isinstance(args, dict):
        return {"action": "block", "message": "ClaimGate: Invalid arguments"}

    try:
        parse_incident_date(str(args.get("incident_date", "")))
        resolve_evidence(
            args.get("claim_id"),
            args.get("evidence_ids", []),
            args.get("incident_fact_ids", []),
        )
    except Exception as exc:  # Fail closed on every parser or registry error.
        return {"action": "block", "message": f"ClaimGate: {exc}"}

    return None


def post_tool_call(*args: Any, **kwargs: Any) -> None:
    """Slice 1 has no post-call work."""

    return None