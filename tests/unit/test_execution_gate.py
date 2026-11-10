from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, "/home/hazeem/.hermes/hermes-agent")

from claim_gate.hooks.execution_gate import pre_tool_call  # noqa: E402
from claim_gate.services.registry import connect  # noqa: E402
from claim_gate.tools import submissions  # noqa: E402


class SliceOneGateTests(unittest.TestCase):
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

    def _claim(self, evidence_ids: list[str], incident_date: str = "2026-02-01", claim_id: str = "CLAIM-101") -> dict[str, object]:
        return {
            "claim_id": claim_id,
            "policy_id": "AUTO-001",
            "incident_date": incident_date,
            "claim_type": "collision",
            "evidence_ids": evidence_ids,
            "incident_fact_ids": [],
        }

    def test_valid_evidence_passes(self) -> None:
        self.assertIsNone(
            pre_tool_call(
                "submit_claim_package",
                self._claim(["EV-001", "EV-002", "EV-003"]),
            )
        )

    def test_forged_ev_999_blocks(self) -> None:
        result = pre_tool_call("submit_claim_package", self._claim(["EV-999"]))
        self.assertEqual(result, {"action": "block", "message": "ClaimGate: Forged ID"})

    def test_cross_claim_ev_004_blocks(self) -> None:
        result = pre_tool_call("submit_claim_package", self._claim(["EV-004"]))
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["action"], "block")
        self.assertIn("Cross-claim theft", result["message"])

    def test_bad_date_blocks(self) -> None:
        result = pre_tool_call(
            "submit_claim_package", self._claim(["EV-001"], incident_date="11-10-2026")
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["action"], "block")
        self.assertTrue(result["message"].startswith("ClaimGate:"))


if __name__ == "__main__":
    unittest.main()
