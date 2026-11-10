#!/usr/bin/env python3
"""Slice 3 pass-gate runner.

Drives the full operator flow against a throwaway Hermes home:

    1. a racing claim is blocked and queued for human review;
    2. ``review-list`` shows the queued claim and its package hash;
    3. ``review-release`` with a wrong hash is refused;
    4. ``review-release`` with the exact hash succeeds;
    5. the released package now passes the gate;
    6. a clean PASS claim submitted twice leaves exactly one ledger row.

Usage::

    python3 scripts/pass_gate_slice3.py
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


RACING = claim(["EV-001", "EV-002", "EV-003"], "2026-11-06", ["FACT-001"])
CLEAN = claim(["EV-001", "EV-002", "EV-003"], "2026-11-06", ["FACT-002"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Use the real Hermes home.")
    args = parser.parse_args()

    temp_home = None
    if not args.live:
        temp_home = tempfile.TemporaryDirectory()
        os.environ["HERMES_HOME"] = temp_home.name

    from claim_gate.cli import review as review_cli
    from claim_gate.hooks.execution_gate import pre_tool_call
    from claim_gate.services.audit import AuditRepository
    from claim_gate.services.registry import connect
    from claim_gate.tools import submissions

    connect()
    submissions.reset_handler_invocation_count()
    repository = AuditRepository()

    import argparse as _argparse

    def cli(argv: list[str]) -> int:
        parser_ = _argparse.ArgumentParser()
        review_cli.setup_argparse(parser_)
        parsed = parser_.parse_args(argv)
        return int(parsed.func(parsed))

    failures = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        if not ok:
            failures += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" -> {detail}" if detail else ""))

    try:
        # 1. Trigger the review queue.
        directive = pre_tool_call("submit_claim_package", RACING)
        check(
            "racing claim is blocked",
            directive == {"action": "block", "message": "ClaimGate: Pending Human Review"},
            json.dumps(directive, sort_keys=True),
        )
        check(
            "handler never ran",
            submissions.handler_invocation_count == 0,
            f"count={submissions.handler_invocation_count}",
        )

        rows = repository.list_reviews(status=None)
        check("exactly one review row", len(rows) == 1, f"rows={len(rows)}")
        package_hash = rows[0]["package_hash"]
        print(f"       package_hash = {package_hash}")

        # 2. The CLI lists the queued claim.
        check("review-list succeeds", cli(["review-list"]) == 0)

        # 3. A wrong hash is refused.
        wrong = "b" * 64
        check("release with a wrong hash is refused", cli(["review-release", "CLAIM-101", "--hash", wrong]) == 1)
        check(
            "the review row is still pending",
            repository.get_review("CLAIM-101")["status"] == "pending",
        )

        # 4. The exact hash releases it.
        check(
            "release with the exact hash succeeds",
            cli(["review-release", "CLAIM-101", "--hash", package_hash]) == 0,
        )
        check(
            "the review row is released",
            repository.get_review("CLAIM-101")["status"] == "released",
        )

        # 5. The released package passes the gate.
        allowed = pre_tool_call("submit_claim_package", RACING)
        check("the released package passes", allowed is None, json.dumps(allowed))

        # 6. A clean claim submitted twice leaves one ledger row.
        check("clean claim passes the gate", pre_tool_call("submit_claim_package", CLEAN) is None)
        first = json.loads(submissions.submit_claim_package(CLEAN))
        second = json.loads(submissions.submit_claim_package(CLEAN))
        check("the first submission is SUBMITTED", first["status"] == "SUBMITTED", first["status"])
        check(
            "the second submission is ALREADY_SUBMITTED",
            second["status"] == "ALREADY_SUBMITTED",
            second["status"],
        )
        ledger = repository.list_claims()
        check("exactly one ledger row", len(ledger) == 1, f"rows={len(ledger)}")
    finally:
        if temp_home is not None:
            temp_home.cleanup()

    print()
    print("gate result:", "PASS" if failures == 0 else f"FAIL ({failures})")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
