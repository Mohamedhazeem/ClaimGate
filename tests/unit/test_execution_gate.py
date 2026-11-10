from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from claim_gate.hooks.execution_gate import pre_tool_call  # noqa: E402

HOOK_LOGGER = "claim_gate.hooks.execution_gate"


class FailClosedHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self._logger = logging.getLogger(HOOK_LOGGER)
        self._old_level = self._logger.level
        self._logger.setLevel(logging.CRITICAL)

    def tearDown(self) -> None:
        self._logger.setLevel(self._old_level)

    def test_non_protected_tool_is_not_blocked(self) -> None:
        self.assertIsNone(
            pre_tool_call("read_file", {"path": "notes.txt"}, task_id="t-1")
        )

    def test_malformed_submission_blocks(self) -> None:
        with patch("claim_gate.hooks.execution_gate._best_effort_block_audit"):
            result = pre_tool_call(
                "submit_claim_package",
                {"claim_id": "CLAIM-101"},
                task_id="t-1",
                tool_call_id="call-bad-args",
            )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["action"], "block")
        self.assertIn("FAIL_CLOSED", result["message"])

    def test_missing_args_blocks(self) -> None:
        with patch("claim_gate.hooks.execution_gate._best_effort_block_audit"):
            result = pre_tool_call(
                "submit_claim_package",
                None,
                task_id="t-1",
                tool_call_id="call-null-args",
            )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["action"], "block")

    def test_invalid_date_blocks(self) -> None:
        with patch("claim_gate.hooks.execution_gate._best_effort_block_audit"):
            result = pre_tool_call(
                "submit_claim_package",
                {
                    "claim_id": "CLAIM-101",
                    "policy_id": "AUTO-001",
                    "incident_date": "2026-1-1",
                    "claim_type": "collision",
                    "evidence_ids": [],
                    "incident_fact_ids": [],
                },
                task_id="t-1",
                tool_call_id="call-bad-date",
            )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["action"], "block")

    def test_unexpected_evaluator_error_blocks(self) -> None:
        with patch(
            "claim_gate.hooks.execution_gate._guard_submission",
            side_effect=RuntimeError("boom"),
        ), patch("claim_gate.hooks.execution_gate._best_effort_block_audit"):
            result = pre_tool_call(
                "submit_claim_package",
                {"claim_id": "CLAIM-101"},
                task_id="t-1",
                tool_call_id="call-boom",
            )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["action"], "block")
        self.assertIn("FAIL_CLOSED", result["message"])


if __name__ == "__main__":
    unittest.main()

