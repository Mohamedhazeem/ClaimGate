"""Slice 2 gate tests.

Two properties matter here:

* every denial returns the exact Hermes block directive
  ``{"action": "block", "message": "..."}`` -- never ``{"error": ...}`` and
  never a silent allow;
* only a deterministic PASS lets the protected handler run.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from claim_gate.hosting import ensure_host_on_path  # noqa: E402
from claim_gate.hooks.execution_gate import pre_tool_call  # noqa: E402
from claim_gate.services.registry import connect, seed_database  # noqa: E402
from claim_gate.tools import submissions  # noqa: E402

# The gate resolves evidence through the Hermes host API. Without a Hermes
# checkout those checks are skipped, never failed.
HOST_AVAILABLE = ensure_host_on_path()


@unittest.skipUnless(
    HOST_AVAILABLE,
    "requires the Hermes host API (plugins.plugin_storage)",
)
class SliceTwoGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_home = os.environ.get("HERMES_HOME")
        self._temp = tempfile.TemporaryDirectory()
        os.environ["HERMES_HOME"] = self._temp.name
        submissions.reset_handler_invocation_count()
        conn = connect()
        seed_database(conn)
        conn.close()

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
        claim_id: str = "CLAIM-101",
        fact_ids: list[str] | None = None,
    ) -> dict[str, object]:
        return {
            "claim_id": claim_id,
            "policy_id": "AUTO-001",
            "incident_date": incident_date,
            "claim_type": "collision",
            "evidence_ids": evidence_ids,
            "incident_fact_ids": fact_ids if fact_ids is not None else [],
        }

    def assert_block(self, result: object) -> dict[str, str]:
        """Assert the result is the only block shape Hermes understands."""

        self.assertIsInstance(result, dict)
        assert isinstance(result, dict)
        self.assertEqual(set(result), {"action", "message"})
        self.assertEqual(result["action"], "block")
        self.assertIsInstance(result["message"], str)
        self.assertTrue(result["message"].strip())
        self.assertNotIn("error", result)
        return result

    # -- the block-shape regression --------------------------------------

    def test_forged_ev_999_blocks(self) -> None:
        result = self.assert_block(
            pre_tool_call("submit_claim_package", self._claim(["EV-999"]))
        )
        self.assertEqual(result["message"], "ClaimGate: Forged ID")

    def test_cross_claim_ev_004_blocks(self) -> None:
        result = self.assert_block(
            pre_tool_call("submit_claim_package", self._claim(["EV-004"]))
        )
        self.assertIn("Cross-claim theft", result["message"])

    def test_unverified_id_blocks(self) -> None:
        result = self.assert_block(
            pre_tool_call("submit_claim_package", self._claim(["EV-005"]))
        )
        self.assertIn("Unverified ID", result["message"])

    def test_bad_date_blocks(self) -> None:
        result = self.assert_block(
            pre_tool_call(
                "submit_claim_package",
                self._claim(["EV-001"], incident_date="11-10-2026"),
            )
        )
        self.assertTrue(result["message"].startswith("ClaimGate:"))

    def test_non_dict_arguments_block(self) -> None:
        self.assert_block(pre_tool_call("submit_claim_package", "not-a-mapping"))

    def test_unknown_tool_is_not_blocked(self) -> None:
        self.assertIsNone(pre_tool_call("some_other_tool", self._claim(["EV-999"])))

    # -- Slice 2 decisions ----------------------------------------------

    def test_missing_repair_estimate_blocks(self) -> None:
        result = self.assert_block(
            pre_tool_call(
                "submit_claim_package",
                self._claim(["EV-001"], fact_ids=["FACT-002"]),
            )
        )
        self.assertEqual(result["message"], "Missing required document: repair_estimate")
        self.assertEqual(submissions.handler_invocation_count, 0)

    def test_racing_activity_requires_review(self) -> None:
        result = self.assert_block(
            pre_tool_call(
                "submit_claim_package",
                self._claim(["EV-001", "EV-002", "EV-003"], fact_ids=["FACT-001"]),
            )
        )
        self.assertEqual(result["message"], "ClaimGate: Pending Human Review")
        self.assertEqual(submissions.handler_invocation_count, 0)

    def test_incident_inside_waiting_period_requires_review(self) -> None:
        result = self.assert_block(
            pre_tool_call(
                "submit_claim_package",
                self._claim(
                    ["EV-001", "EV-002", "EV-003"],
                    incident_date="2026-01-10",
                    fact_ids=["FACT-002"],
                ),
            )
        )
        self.assertEqual(result["message"], "ClaimGate: Pending Human Review")
        self.assertEqual(submissions.handler_invocation_count, 0)

    def test_missing_activity_fact_requires_review(self) -> None:
        result = self.assert_block(
            pre_tool_call(
                "submit_claim_package",
                self._claim(["EV-001", "EV-002", "EV-003"]),
            )
        )
        self.assertEqual(result["message"], "ClaimGate: Pending Human Review")
        self.assertEqual(submissions.handler_invocation_count, 0)

    def test_complete_package_reaches_the_handler(self) -> None:
        args = self._claim(["EV-001", "EV-002", "EV-003"], fact_ids=["FACT-002"])
        result = pre_tool_call("submit_claim_package", args)
        self.assertIsNone(result)
        # Hermes dispatches the handler only when the hook allows the call.
        submissions.submit_claim_package(args)
        self.assertEqual(submissions.handler_invocation_count, 1)

    def test_model_supplied_status_fields_are_ignored(self) -> None:
        # The model cannot make a claim ready by asserting it is verified.
        args = self._claim(["EV-001"], fact_ids=["FACT-002"])
        args["status"] = "verified"  # type: ignore[assignment]
        args["verified"] = True  # type: ignore[assignment]
        result = self.assert_block(pre_tool_call("submit_claim_package", args))
        self.assertEqual(result["message"], "Missing required document: repair_estimate")


if __name__ == "__main__":
    unittest.main()
