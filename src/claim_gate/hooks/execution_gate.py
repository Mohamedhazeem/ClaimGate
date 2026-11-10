"""Slice 0 interception gate.

Only `pre_tool_call` is active. It hardcodes the single forged-ID check
required to prove interception before any database or policy logic exists.
"""

from __future__ import annotations

from typing import Any

__all__ = ["PROTECTED_TOOL", "post_tool_call", "pre_tool_call"]

PROTECTED_TOOL = "submit_claim_package"


def pre_tool_call(tool_name: str, args: Any = None, task_id: str = "", **kwargs: Any) -> dict[str, str] | None:
    """Block the protected tool only when the hardcoded forged ID is present."""

    if tool_name != PROTECTED_TOOL:
        return None

    evidence_ids = []
    if isinstance(args, dict):
        evidence_ids = args.get("evidence_ids", [])

    if "EV-999" in evidence_ids:
        return {"action": "block", "message": "Forged ID"}

    return None


def post_tool_call(*args: Any, **kwargs: Any) -> None:
    """Slice 0 has no post-call work."""

    return None
