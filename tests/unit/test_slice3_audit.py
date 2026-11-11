"""Slice 3 tests: audit, the human-review queue, ledger idempotency, and the
operator CLI release flow.

The invariants under test:

* a HUMAN_REVIEW package is queued exactly once and stays queued until an
  operator acts;
* a PASS package is appended to the ledger exactly once, and a repeated
  submission of the same package is an idempotent replay;
* the ledger is append-only: a changed package for an already-submitted
  claim is refused, never written over;
* an operator release is bound to one exact package hash, and the agent has
  no tool that can produce one.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from claim_gate.cli import review as review_cli  # noqa: E402
from claim_gate.hosting import ensure_host_on_path  # noqa: E402
from claim_gate.hooks.execution_gate import post_tool_call, pre_tool_call  # noqa: E402
from claim_gate.services.audit import (  # noqa: E402
    AuditRepository,
    compute_package_hash,
    generate_package_hash,
)
from claim_gate.services.registry import connect, seed_database  # noqa: E402
from claim_gate.tools import submissions  # noqa: E402

# The audit trail and the review queue live in the Hermes plugin database.
# Without a Hermes checkout those checks are skipped, never failed.
HOST_AVAILABLE = ensure_host_on_path()

HASH_A = "a" * 64
HASH_B = "b" * 64


@unittest.skipUnless(
    HOST_AVAILABLE,
    "requires the Hermes host API (plugins.plugin_storage)",
)
class SliceThreeBase(unittest.TestCase):
    def setUp(self) -> None:
        self._old_home = os.environ.get("HERMES_HOME")
        self._temp = tempfile.TemporaryDirectory()
        os.environ["HERMES_HOME"] = self._temp.name
        submissions.reset_handler_invocation_count()
        conn = connect()
        seed_database(conn)
        conn.close()
        self.repository = AuditRepository()

    def tearDown(self) -> None:
        if self._old_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._old_home
        self._temp.cleanup()

    def _claim(
        self,
        evidence_ids: list[str],
        incident_date: str = "2026-11-06",
        fact_ids: list[str] | None = None,
        claim_id: str = "CLAIM-101",
    ) -> dict[str, object]:
        return {
            "claim_id": claim_id,
            "policy_id": "AUTO-001",
            "incident_date": incident_date,
            "claim_type": "collision",
            "evidence_ids": evidence_ids,
            "incident_fact_ids": fact_ids if fact_ids is not None else [],
        }

    def _run_cli(self, argv: list[str]) -> int:
        import argparse
        import contextlib
        import io

        parser = argparse.ArgumentParser()
        review_cli.setup_argparse(parser)
        args = parser.parse_args(argv)
        # The CLI prints to stdout; capture it so the test output stays clean.
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            return int(args.func(args))


class HumanReviewQueueTests(SliceThreeBase):
    def test_human_review_inserts_exactly_one_queue_row(self) -> None:
        result = pre_tool_call(
            "submit_claim_package",
            self._claim(["EV-001", "EV-002", "EV-003"], fact_ids=["FACT-001"]),
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["action"], "block")
        self.assertEqual(result["message"], "ClaimGate: Pending Human Review")

        rows = self.repository.list_reviews(status=None)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["claim_id"], "CLAIM-101")
        self.assertEqual(rows[0]["status"], "pending")
        self.assertEqual(len(rows[0]["package_hash"]), 64)
        # The specific reason stays with the operator, not with the model.
        self.assertIn("racing", rows[0]["reason"])

    def test_repeated_review_keeps_one_row(self) -> None:
        args = self._claim(["EV-001", "EV-002", "EV-003"], fact_ids=["FACT-001"])
        for _ in range(3):
            pre_tool_call("submit_claim_package", args)
        rows = self.repository.list_reviews(status=None)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "pending")

    def test_block_does_not_queue_a_review(self) -> None:
        pre_tool_call(
            "submit_claim_package",
            self._claim(["EV-001"], fact_ids=["FACT-001"]),
        )
        self.assertEqual(self.repository.list_reviews(status=None), [])


class LedgerIdempotencyTests(SliceThreeBase):
    def test_same_pass_claim_twice_inserts_one_ledger_row(self) -> None:
        args = self._claim(["EV-001", "EV-002", "EV-003"], fact_ids=["FACT-002"])

        first = pre_tool_call("submit_claim_package", args)
        self.assertIsNone(first)
        first_result = json.loads(submissions.submit_claim_package(args))
        self.assertEqual(first_result["status"], "SUBMITTED")

        second = pre_tool_call("submit_claim_package", args)
        self.assertIsNone(second)
        second_result = json.loads(submissions.submit_claim_package(args))
        self.assertEqual(second_result["status"], "ALREADY_SUBMITTED")
        self.assertTrue(second_result["ok"])

        rows = self.repository.list_claims()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["claim_id"], "CLAIM-101")
        self.assertEqual(len(rows[0]["package_hash"]), 64)

    def test_ledger_row_is_not_overwritten_by_a_changed_package(self) -> None:
        args = self._claim(["EV-001", "EV-002", "EV-003"], fact_ids=["FACT-002"])
        pre_tool_call("submit_claim_package", args)
        submissions.submit_claim_package(args)

        original = self.repository.list_claims()[0]["package_hash"]

        # Same claim, different package: the ledger must refuse the change.
        changed = self._claim(
            ["EV-001", "EV-002", "EV-003"], incident_date="2026-12-01", fact_ids=["FACT-002"]
        )
        result = pre_tool_call("submit_claim_package", changed)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["action"], "block")

        rows = self.repository.list_claims()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["package_hash"], original)

    def test_ledger_is_append_only_at_the_database_level(self) -> None:
        import sqlite3

        args = self._claim(["EV-001", "EV-002", "EV-003"], fact_ids=["FACT-002"])
        pre_tool_call("submit_claim_package", args)
        submissions.submit_claim_package(args)

        with sqlite3.connect(self._db_path()) as conn:
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute(
                    "UPDATE claim_ledger SET package_hash = ? WHERE claim_id = ?",
                    (HASH_B, "CLAIM-101"),
                )

    def _db_path(self) -> str:
        from hermes_constants import get_hermes_home

        return str(get_hermes_home() / "plugin-data" / "claim_gate" / "data.db")


class AuditTrailTests(SliceThreeBase):
    def test_block_is_audited(self) -> None:
        pre_tool_call("submit_claim_package", self._claim(["EV-999"]))
        rows = self.repository.get_audit("CLAIM-101")
        self.assertTrue(rows)
        self.assertEqual(rows[0]["decision"], "BLOCK")

    def test_human_review_is_audited(self) -> None:
        pre_tool_call(
            "submit_claim_package",
            self._claim(["EV-001", "EV-002", "EV-003"], fact_ids=["FACT-001"]),
        )
        rows = self.repository.get_audit("CLAIM-101")
        self.assertTrue(rows)
        self.assertEqual(rows[0]["decision"], "HUMAN_REVIEW")

    def test_pass_is_audited_and_the_execution_is_recorded(self) -> None:
        args = self._claim(["EV-001", "EV-002", "EV-003"], fact_ids=["FACT-002"])
        pre_tool_call("submit_claim_package", args)
        result = submissions.submit_claim_package(args)

        rows = self.repository.get_audit("CLAIM-101")
        self.assertTrue(rows)
        self.assertEqual(rows[0]["decision"], "PASS")
        self.assertIsNone(rows[0]["execution_success"])

        post_tool_call(
            "submit_claim_package",
            args,
            result,
            tool_call_id=rows[0]["tool_call_id"],
        )
        rows = self.repository.get_audit("CLAIM-101")
        self.assertEqual(rows[0]["execution_success"], 1)

    def test_failed_execution_is_recorded(self) -> None:
        args = self._claim(["EV-001", "EV-002", "EV-003"], fact_ids=["FACT-002"])
        pre_tool_call("submit_claim_package", args)
        post_tool_call(
            "submit_claim_package",
            args,
            json.dumps({"ok": False, "status": "FAILED"}),
            tool_call_id="call-failed",
        )
        rows = self.repository.get_audit("CLAIM-101")
        self.assertTrue(rows)
        self.assertEqual(rows[0]["execution_success"], 0)


class OperatorReleaseTests(SliceThreeBase):
    def _queue_a_review(self) -> str:
        pre_tool_call(
            "submit_claim_package",
            self._claim(["EV-001", "EV-002", "EV-003"], fact_ids=["FACT-001"]),
        )
        rows = self.repository.list_reviews(status=None)
        self.assertEqual(len(rows), 1)
        return str(rows[0]["package_hash"])

    def _review_status(self, claim_id: str = "CLAIM-101") -> str:
        review = self.repository.get_review(claim_id)
        self.assertIsNotNone(review)
        assert review is not None
        return str(review["status"])

    def test_release_requires_the_exact_hash(self) -> None:
        package_hash = self._queue_a_review()

        # A wrong hash is refused and leaves the row pending.
        self.assertEqual(self._run_cli(["review-release", "CLAIM-101", "--hash", HASH_B]), 1)
        self.assertEqual(self._review_status(), "pending")

        # The exact hash releases it.
        self.assertEqual(
            self._run_cli(["review-release", "CLAIM-101", "--hash", package_hash]), 0
        )
        self.assertEqual(self._review_status(), "released")

    def test_release_of_an_unknown_claim_fails(self) -> None:
        self.assertEqual(self._run_cli(["review-release", "CLAIM-999", "--hash", HASH_A]), 1)

    def test_release_of_a_malformed_hash_fails(self) -> None:
        self.assertEqual(self._run_cli(["review-release", "CLAIM-101", "--hash", "nope"]), 2)

    def test_released_package_passes_the_gate(self) -> None:
        package_hash = self._queue_a_review()
        self.repository.release_human_review("CLAIM-101", package_hash)

        args = self._claim(["EV-001", "EV-002", "EV-003"], fact_ids=["FACT-001"])
        self.assertIsNone(pre_tool_call("submit_claim_package", args))

    def test_release_does_not_survive_a_changed_package(self) -> None:
        package_hash = self._queue_a_review()
        self.repository.release_human_review("CLAIM-101", package_hash)

        # The AI changes the package: a different fingerprint, so the old
        # release no longer applies.
        changed = self._claim(
            ["EV-001", "EV-002", "EV-003"],
            incident_date="2026-12-01",
            fact_ids=["FACT-001"],
        )
        result = pre_tool_call("submit_claim_package", changed)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["action"], "block")

        # The queue entry is reset to pending for the new package.
        review = self.repository.get_review("CLAIM-101")
        assert review is not None
        self.assertEqual(review["status"], "pending")
        self.assertNotEqual(review["package_hash"], package_hash)

    def test_review_list_shows_the_pending_claim(self) -> None:
        self._queue_a_review()
        self.assertEqual(self._run_cli(["review-list"]), 0)

    def test_ledger_list_shows_the_submitted_claim(self) -> None:
        args = self._claim(["EV-001", "EV-002", "EV-003"], fact_ids=["FACT-002"])
        pre_tool_call("submit_claim_package", args)
        submissions.submit_claim_package(args)
        self.assertEqual(self._run_cli(["ledger-list"]), 0)


class PackageHashTests(SliceThreeBase):
    def test_generate_package_hash_is_canonical(self) -> None:
        first = generate_package_hash(
            "CLAIM-101",
            HASH_A,
            "2026-11-06",
            "collision",
            ["EV-002", "EV-001"],
            ["FACT-002"],
        )
        second = generate_package_hash(
            "CLAIM-101",
            HASH_A,
            "2026-11-06",
            "collision",
            ["EV-001", "EV-002"],
            ["FACT-002"],
        )
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)

    def test_generate_package_hash_changes_with_every_input(self) -> None:
        base = generate_package_hash(
            "CLAIM-101", HASH_A, "2026-11-06", "collision", ["EV-001"], []
        )
        variants = [
            generate_package_hash(
                "CLAIM-102", HASH_A, "2026-11-06", "collision", ["EV-001"], []
            ),
            generate_package_hash(
                "CLAIM-101", HASH_B, "2026-11-06", "collision", ["EV-001"], []
            ),
            generate_package_hash(
                "CLAIM-101", HASH_A, "2026-11-07", "collision", ["EV-001"], []
            ),
            generate_package_hash(
                "CLAIM-101", HASH_A, "2026-11-06", "liability", ["EV-001"], []
            ),
            generate_package_hash(
                "CLAIM-101", HASH_A, "2026-11-06", "collision", ["EV-002"], []
            ),
            generate_package_hash(
                "CLAIM-101", HASH_A, "2026-11-06", "collision", ["EV-001"], ["FACT-001"]
            ),
        ]
        for variant in variants:
            self.assertNotEqual(base, variant)


if __name__ == "__main__":
    unittest.main()
