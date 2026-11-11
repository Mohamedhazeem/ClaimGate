#!/usr/bin/env python3
"""Play the ClaimGate 90-second demo storyboard, end to end.

This drives the *real* gate (``pre_tool_call``), the *real* handler, and the
*real* operator CLI against the real SQLite registry. Nothing is mocked.

Usage::

    python3 demo/reset.py            # start from a clean fixture
    python3 demo/run_demo.py         # replay the storyboard

    python3 demo/run_demo.py --temp  # use a throwaway Hermes home
    python3 demo/run_demo.py --live  # use the live Hermes home (default)

Each beat prints the action, the exact Hermes directive the agent receives,
and the judge takeaway. A beat that does not behave as specified fails the
run.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

HERMES_AGENT = Path.home() / ".hermes" / "hermes-agent"
if HERMES_AGENT.is_dir():
    sys.path.insert(0, str(HERMES_AGENT))

# The gate logs a warning for every registry violation. That is correct
# behavior, but it would interleave with the storyboard and obscure it.
logging.getLogger("claim_gate").setLevel(logging.ERROR)

DOCS = ["EV-001", "EV-002", "EV-003"]


def claim(
    evidence_ids: list[str],
    incident_date: str,
    fact_ids: list[str] | None = None,
) -> dict[str, object]:
    """Build the model-facing claim payload.

    Only references. The model has no way to state a status or a document
    type, and ClaimGate ignores anything beyond these six fields.
    """

    return {
        "claim_id": "CLAIM-101",
        "policy_id": "AUTO-001",
        "incident_date": incident_date,
        "claim_type": "collision",
        "evidence_ids": evidence_ids,
        "incident_fact_ids": fact_ids or [],
    }


class Demo:
    def __init__(self) -> None:
        from claim_gate.cli import review as review_cli
        from claim_gate.hooks.execution_gate import pre_tool_call
        from claim_gate.services.audit import AuditRepository
        from claim_gate.services.registry import connect, seed_database
        from claim_gate.tools import submissions

        self.pre_tool_call = pre_tool_call
        self.submissions = submissions
        self.repository = AuditRepository()
        self.review_cli = review_cli
        # The demo seeds its own fixture explicitly. Runtime code never seeds.
        conn = connect()
        seed_database(conn)
        conn.close()

    # -- helpers -------------------------------------------------------

    def gate(self, args: dict[str, object]) -> dict[str, str] | None:
        """Run the interception gate exactly as Hermes would."""

        return self.pre_tool_call("submit_claim_package", args)

    def dispatch(self, args: dict[str, object]) -> str:
        """Run the protected handler exactly as Hermes would after a PASS."""

        return self.submissions.submit_claim_package(args)

    def cli(self, argv: list[str]) -> int:
        parser = argparse.ArgumentParser()
        self.review_cli.setup_argparse(parser)
        parsed = parser.parse_args(argv)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = int(parsed.func(parsed))
        self.last_cli_output = buffer.getvalue()
        return code

    last_cli_output = ""

    def review_status(self, claim_id: str = "CLAIM-101") -> str:
        """Return the review status, asserting that a row exists."""

        review = self.repository.get_review(claim_id)
        assert review is not None, f"no review row for {claim_id}"
        return str(review["status"])

    def beat(self, title: str, takeaway: str) -> None:
        print()
        print(f"=== {title} " + "=" * max(0, 60 - len(title)))
        print(f"    takeaway: {takeaway}")

    def expect(self, label: str, actual: object, expected: object) -> bool:
        ok = actual == expected
        print(f"    [{'OK' if ok else 'FAIL'}] {label}")
        if not ok:
            print(f"           expected: {expected!r}")
            print(f"           actual:   {actual!r}")
        return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--temp", action="store_true", help="Use a throwaway Hermes home."
    )
    group.add_argument(
        "--live", action="store_true", help="Use the live Hermes home (default)."
    )
    args = parser.parse_args()

    temp_home = None
    if args.temp:
        temp_home = tempfile.TemporaryDirectory()
        os.environ["HERMES_HOME"] = temp_home.name

    failures = 0
    try:
        demo = Demo()

        # ---- 0-10s: the operator controls the evidence ----------------
        demo.beat(
            "0-10s  The operator controls the evidence",
            "the AI cannot fake what it does not have",
        )
        policy_path = ROOT / "policies" / "auto_policy.json"
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        coverage = policy["coverages"]["collision"]
        print(f"    policy: {policy_path.name} ({policy['policy_id']})")
        print(f"      required_documents: {', '.join(coverage['required_documents'])}")
        print(f"      required_verified_facts: {', '.join(coverage['required_verified_facts'])}")
        print(f"      review_trigger: activity in {coverage['review_triggers'][0]['values']}")
        print(f"      coverage_available_on: {coverage['waiting_period']['coverage_available_on']}")

        from claim_gate.services.registry import connect

        conn = connect()
        print("    evidence_registry:")
        for row in conn.execute(
            "SELECT evidence_id, claim_id, document_type, status "
            "FROM evidence_registry ORDER BY evidence_id"
        ):
            print(f"      {row[0]:<10} {row[1]:<12} {row[2]:<18} {row[3]}")
        conn.close()

        # ---- 10-25s: a missing document is blocked --------------------
        demo.beat(
            "10-25s  Submit a claim for Nov 4 with EV-001, EV-002",
            "BLOCKED. Missing repair_estimate (EV-003).",
        )
        directive = demo.gate(claim(["EV-001", "EV-002"], "2026-11-04", ["FACT-002"]))
        print(f"    agent receives: {json.dumps(directive, sort_keys=True)}")
        failures += 0 if demo.expect(
            "missing document blocks",
            directive,
            {"action": "block", "message": "Missing required document: repair_estimate"},
        ) else 1

        # ---- 25-40s: an invented ID is blocked ------------------------
        demo.beat(
            "25-40s  Submit a claim for Nov 5 with EV-001..EV-003 and EV-999",
            "BLOCKED. The AI tried to invent evidence (EV-999).",
        )
        directive = demo.gate(claim(DOCS + ["EV-999"], "2026-11-05", ["FACT-002"]))
        print(f"    agent receives: {json.dumps(directive, sort_keys=True)}")
        failures += 0 if demo.expect(
            "forged ID blocks",
            directive,
            {"action": "block", "message": "ClaimGate: Forged ID"},
        ) else 1

        # ---- 40-55s: a waiting period needs a human -------------------
        demo.beat(
            "40-55s  Submit a claim for Jan 10 with all IDs",
            "HUMAN REVIEW. The incident is inside the waiting period.",
        )
        directive = demo.gate(claim(DOCS, "2026-01-10", ["FACT-002"]))
        print(f"    agent receives: {json.dumps(directive, sort_keys=True)}")
        failures += 0 if demo.expect(
            "waiting period blocks",
            directive,
            {"action": "block", "message": "ClaimGate: Pending Human Review"},
        ) else 1
        rows = demo.repository.list_reviews(status=None)
        failures += 0 if demo.expect("exactly one review row", len(rows), 1) else 1

        # ---- 55-70s: the operator holds the only approval path ---------
        demo.beat(
            "55-70s  Show the operator CLI",
            "the AI cannot approve its own claim",
        )
        demo.cli(["review-list"])
        for line in demo.last_cli_output.splitlines():
            print(f"    {line}")

        # ---- 70-85s: a complete package passes ------------------------
        demo.beat(
            "70-85s  Submit a claim for Nov 6 with all IDs",
            "ALLOWED. The handler executes and the claim ledger updates.",
        )
        payload = claim(DOCS, "2026-11-06", ["FACT-002"])
        directive = demo.gate(payload)
        print(f"    agent receives: {json.dumps(directive, sort_keys=True)}")
        failures += 0 if demo.expect("complete package passes", directive, None) else 1
        result = json.loads(demo.dispatch(payload))
        print(f"    handler returns: {json.dumps(result, sort_keys=True)}")
        failures += 0 if demo.expect("handler ran", result["status"], "SUBMITTED") else 1
        failures += 0 if demo.expect(
            "one ledger row", len(demo.repository.list_claims()), 1
        ) else 1

        # -- the idempotency beat --------------------------------------
        print()
        print("    submitting the very same claim again...")
        result = json.loads(demo.dispatch(payload))
        print(f"    handler returns: {json.dumps(result, sort_keys=True)}")
        failures += 0 if demo.expect(
            "repeat is idempotent", result["status"], "ALREADY_SUBMITTED"
        ) else 1
        failures += 0 if demo.expect(
            "still one ledger row", len(demo.repository.list_claims()), 1
        ) else 1

        # ---- the immutable fingerprint beat ----------------------------
        demo.beat(
            "Extended  The immutable fingerprint",
            "a release is bound to one exact package hash",
        )
        review = demo.repository.get_review("CLAIM-101")
        assert review is not None
        package_hash = review["package_hash"]
        print(f"    queued package_hash: {package_hash}")

        code = demo.cli(["review-release", "CLAIM-101", "--hash", "b" * 64])
        print(f"    release with a wrong hash -> exit {code}")
        failures += 0 if demo.expect("wrong hash is refused", code, 1) else 1
        failures += 0 if demo.expect(
            "the row stays pending",
            demo.review_status(),
            "pending",
        ) else 1

        code = demo.cli(["review-release", "CLAIM-101", "--hash", package_hash])
        print(f"    release with the exact hash -> exit {code}")
        failures += 0 if demo.expect("exact hash is released", code, 0) else 1
        failures += 0 if demo.expect(
            "the row is released",
            demo.review_status(),
            "released",
        ) else 1

        # ---- 85-90s: the plugin is clean ------------------------------
        demo.beat(
            "85-90s  Validate the plugin",
            "the plugin is clean and valid",
        )
        print("    run: hermes plugins validate . --json")
        print("    run: hermes plugins doctor . --ci")
    finally:
        if temp_home is not None:
            temp_home.cleanup()

    print()
    print("demo result:", "PASS" if failures == 0 else f"FAIL ({failures})")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
