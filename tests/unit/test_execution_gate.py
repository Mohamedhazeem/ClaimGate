from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from claim_gate.hooks.execution_gate import pre_tool_call  # noqa: E402
from claim_gate.tools import submissions  # noqa: E402


class InterceptionGateTests(unittest.TestCase):
    def setUp(self) -> None:
        submissions.reset_handler_invocation_count()

    def test_non_protected_tool_is_allowed(self) -> None:
        self.assertIsNone(pre_tool_call("read_file", {"path": "notes.txt"}))

    def test_ev_001_reaches_handler(self) -> None:
        self.assertIsNone(
            pre_tool_call(
                "submit_claim_package",
                {"evidence_ids": ["EV-001"]},
            )
        )
        submissions.submit_claim_package({"evidence_ids": ["EV-001"]})
        self.assertEqual(submissions.handler_invocation_count, 1)

    def test_ev_999_is_blocked_and_does_not_reach_handler(self) -> None:
        result = pre_tool_call(
            "submit_claim_package",
            {"evidence_ids": ["EV-999"]},
        )
        self.assertEqual(result, {"action": "block", "message": "Forged ID"})
        self.assertEqual(submissions.handler_invocation_count, 0)


if __name__ == "__main__":
    unittest.main()
