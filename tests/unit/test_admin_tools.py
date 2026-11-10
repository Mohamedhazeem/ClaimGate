"""Slice 2 tests for the read-only admin tools.

``claim_check`` is a dry run of the same trusted path the gate uses, and
``claim_policy_get`` exposes the active operator policy. Neither tool can
write to a registry, release a claim, or mutate review state.
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
sys.path.insert(0, "/home/hazeem/.hermes/hermes-agent")

from claim_gate.services.registry import connect  # noqa: E402
from claim_gate.tools import admin, submissions  # noqa: E402


class AdminToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_home = os.environ.get("HERMES_HOME")
        self._temp = tempfile.TemporaryDirectory()
        os.environ["HERMES_HOME"] = self._temp.name
        submissions.reset_handler_invocation_count()
        connect()  # initialize and seed the default database

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
    ) -> dict[str, object]:
        return {
            "claim_id": "CLAIM-101",
            "policy_id": "AUTO-001",
            "incident_date": incident_date,
            "claim_type": "collision",
            "evidence_ids": evidence_ids,
            "incident_fact_ids": fact_ids if fact_ids is not None else [],
        }

    def test_claim_check_reports_pass(self) -> None:
        payload = json.loads(
            admin.claim_check(
                self._claim(["EV-001", "EV-002", "EV-003"], fact_ids=["FACT-002"])
            )
        )
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["decision"], "PASS")
        self.assertEqual(len(payload["package_hash"]), 64)
        # A dry run never submits anything.
        self.assertEqual(submissions.handler_invocation_count, 0)

    def test_claim_check_reports_block(self) -> None:
        payload = json.loads(admin.claim_check(self._claim(["EV-001"], fact_ids=["FACT-002"])))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["decision"], "BLOCK")
        self.assertEqual(payload["message"], "Missing required document: repair_estimate")

    def test_claim_check_reports_human_review(self) -> None:
        payload = json.loads(
            admin.claim_check(
                self._claim(["EV-001", "EV-002", "EV-003"], fact_ids=["FACT-001"])
            )
        )
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["decision"], "HUMAN_REVIEW")
        self.assertEqual(payload["message"], "Review required: activity is racing")

    def test_claim_check_blocks_forged_ids(self) -> None:
        payload = json.loads(admin.claim_check(self._claim(["EV-999"])))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["decision"], "BLOCK")
        self.assertEqual(payload["code"], "EVIDENCE_NOT_FOUND")

    def test_claim_policy_get_returns_the_active_policy(self) -> None:
        payload = json.loads(admin.claim_policy_get({}))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["policy_id"], "AUTO-001")
        self.assertEqual(len(payload["policy_hash"]), 64)
        coverage = payload["policy"]["coverages"]["collision"]
        self.assertEqual(
            sorted(coverage["required_documents"]),
            ["photo_evidence", "police_report", "repair_estimate"],
        )
        self.assertEqual(coverage["required_verified_facts"], ["activity"])
        self.assertEqual(
            coverage["waiting_period"]["coverage_available_on"], "2026-01-31"
        )

    def test_claim_audit_is_read_only(self) -> None:
        payload = json.loads(admin.claim_audit({"claim_id": "CLAIM-101"}))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["audit"], [])
        self.assertIsNone(payload["review"])


if __name__ == "__main__":
    unittest.main()
