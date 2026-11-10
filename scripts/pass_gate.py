#!/usr/bin/env python3
"""Slice 2 pass-gate runner.

Runs the four gate scenarios from the ClaimGate slice plan against the real
``pre_tool_call`` hook and prints the exact Hermes directive each one
produces.

Usage::

    python3 scripts/pass_gate.py            # uses a throwaway Hermes home
    python3 scripts/pass_gate.py --live     # uses the real Hermes home

The scenarios are:

    Missing Doc      EV-001 only, 2026-11-06        -> BLOCK
    Racing Trigger   EV-001..EV-003 + FACT-001      -> HUMAN_REVIEW (blocked)
    Waiting Period   EV-001..EV-003, 2026-01-10     -> HUMAN_REVIEW (blocked)
    Clean Pass       EV-001..EV-003 + FACT-002      -> handler runs

Note: since Slice 3, a HUMAN_REVIEW decision is reported to the model as the
generic ``ClaimGate: Pending Human Review``. The specific reason (missing
document, racing activity, waiting period) is kept for the operator in the
review queue and the audit trail, and is returned by ``claim_check``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

HERMES_AGENT = Path.home() / ".hermes" / "hermes-agent"
if HERMES_AGENT.is_dir():
    sys.path.insert(0, str(HERMES_AGENT))


def claim(
    evidence_ids: list[str],
    incident_date: str,
    fact_ids: list[str] | None = None,
) -> dict[str, object]:
    return {
        "claim_id": "CLAIM-101",
        "policy_id": "AUTO-001",
        "incident_date": incident_date,
        "claim_type": "collision",
        "evidence_ids": evidence_ids,
        "incident_fact_ids": fact_ids or [],
    }


SCENARIOS = [
    (
        "Missing Doc",
        claim(["EV-001"], "2026-11-06", ["FACT-002"]),
        {"action": "block", "message": "Missing required document: repair_estimate"},
    ),
    (
        "Racing Trigger",
        claim(["EV-001", "EV-002", "EV-003"], "2026-11-06", ["FACT-001"]),
        {"action": "block", "message": "ClaimGate: Pending Human Review"},
    ),
    (
        "Waiting Period",
        claim(["EV-001", "EV-002", "EV-003"], "2026-01-10", ["FACT-002"]),
        {"action": "block", "message": "ClaimGate: Pending Human Review"},
    ),
    (
        "Clean Pass",
        claim(["EV-001", "EV-002", "EV-003"], "2026-11-06", ["FACT-002"]),
        None,
    ),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help="Use the real Hermes home instead of a throwaway one.",
    )
    args = parser.parse_args()

    temp_home = None
    if not args.live:
        temp_home = tempfile.TemporaryDirectory()
        os.environ["HERMES_HOME"] = temp_home.name

    from claim_gate.hooks.execution_gate import pre_tool_call
    from claim_gate.services.registry import connect
    from claim_gate.tools import submissions

    connect()  # initialize and seed the trusted registries
    submissions.reset_handler_invocation_count()

    failures = 0
    try:
        for name, payload, expected in SCENARIOS:
            directive = pre_tool_call("submit_claim_package", payload)
            if directive is None:
                # Hermes dispatches the handler only when the hook allows it.
                submissions.submit_claim_package(payload)
            ok = directive == expected
            failures += 0 if ok else 1
            rendered = json.dumps(directive, sort_keys=True)
            print(f"[{'PASS' if ok else 'FAIL'}] {name}: {rendered}")
            if directive is None:
                print(
                    f"       handler_invocation_count="
                    f"{submissions.handler_invocation_count}"
                )
    finally:
        if temp_home is not None:
            temp_home.cleanup()

    print()
    print("gate result:", "PASS" if failures == 0 else f"FAIL ({failures})")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
