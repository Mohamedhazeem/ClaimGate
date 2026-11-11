"""Regression tests for arguments a model maps out of natural language.

A model reads prose and fills in the tool arguments. That mapping is the one
place where a language model's output touches ClaimGate, so the artifacts a
prose mapping can plausibly produce are worth pinning down:

* a model that copies "it's verified" out of the prose into the arguments;
* a model that sends a comma-separated string instead of an array;
* a model that sends "November 6, 2026" instead of an ISO date;
* a model that invents fields the schema does not declare.

Every one of those must either be ignored outright or be rejected. None of
them may widen what the plugin trusts.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from claim_gate.domain.claim import ValidationError, normalize_claim  # noqa: E402
from claim_gate.domain.policy import load_policy  # noqa: E402
from claim_gate.hosting import ensure_host_on_path  # noqa: E402
from claim_gate.services.audit import compute_package_hash  # noqa: E402
from claim_gate.services.registry import connect, seed_database  # noqa: E402

HOST_AVAILABLE = ensure_host_on_path()

BASE_ARGS = {
    "claim_id": "CLAIM-101",
    "policy_id": "AUTO-001",
    "incident_date": "2026-11-06",
    "claim_type": "collision",
    "evidence_ids": ["EV-001", "EV-002", "EV-003"],
    "incident_fact_ids": ["FACT-002"],
}


class ProseArtifactsAreIgnoredTests(unittest.TestCase):
    """A model's own assertions and inventions carry no weight."""

    def test_verification_fields_are_not_captured(self) -> None:
        args = dict(BASE_ARGS)
        args.update(
            {
                "status": "verified",
                "verified": True,
                "verified_by": "adjuster@example",
                "document_type": "police_report",
                "confidence": 0.99,
            }
        )
        self.assertEqual(normalize_claim(args).to_dict(), normalize_claim(BASE_ARGS).to_dict())

    def test_unknown_fields_are_not_captured(self) -> None:
        args = dict(BASE_ARGS)
        args.update({"notes": "rushing to close", "priority": "high", "agent": "gpt"})
        self.assertEqual(normalize_claim(args).to_dict(), normalize_claim(BASE_ARGS).to_dict())

    def test_verification_fields_do_not_change_the_fingerprint(self) -> None:
        policy = load_policy()
        args = dict(BASE_ARGS)
        args.update({"status": "verified", "verified_by": "adjuster@example"})

        base_hash = compute_package_hash(
            normalize_claim(BASE_ARGS), policy.source_hash, (), ()
        )
        noisy_hash = compute_package_hash(
            normalize_claim(args), policy.source_hash, (), ()
        )
        self.assertEqual(base_hash, noisy_hash)

    def test_id_order_does_not_change_the_fingerprint(self) -> None:
        policy = load_policy()
        reordered = dict(BASE_ARGS, evidence_ids=["EV-003", "EV-001", "EV-002"])
        self.assertEqual(
            compute_package_hash(normalize_claim(BASE_ARGS), policy.source_hash, (), ()),
            compute_package_hash(normalize_claim(reordered), policy.source_hash, (), ()),
        )

    def test_duplicate_ids_are_collapsed(self) -> None:
        duplicated = dict(BASE_ARGS, evidence_ids=["EV-001", "EV-001", "EV-002"])
        self.assertEqual(
            normalize_claim(duplicated).evidence_ids,
            normalize_claim(dict(BASE_ARGS, evidence_ids=["EV-001", "EV-002"])).evidence_ids,
        )


class ProseArtifactsAreRejectedTests(unittest.TestCase):
    """Malformed prose mappings are rejected, never coerced."""

    def assert_rejected(self, args: object, field: str) -> None:
        with self.assertRaises(ValidationError) as raised:
            normalize_claim(args)
        self.assertIn(field, str(raised.exception))

    def test_comma_separated_evidence_is_rejected(self) -> None:
        self.assert_rejected(
            dict(BASE_ARGS, evidence_ids="EV-001, EV-002, EV-003"), "evidence_ids"
        )

    def test_single_evidence_id_string_is_rejected(self) -> None:
        self.assert_rejected(dict(BASE_ARGS, evidence_ids="EV-001"), "evidence_ids")

    def test_comma_separated_facts_are_rejected(self) -> None:
        self.assert_rejected(
            dict(BASE_ARGS, incident_fact_ids="FACT-001, FACT-002"), "incident_fact_ids"
        )

    def test_human_readable_dates_are_rejected(self) -> None:
        for value in (
            "November 6, 2026",
            "6 November 2026",
            "06/11/2026",
            "11/06/2026",
            "2026-11-06T10:00:00Z",
            "2026-11-06T10:00:00",
            "2026-11-06 ",
            " 2026-11-06",
            "20261106",
            "2026-13-01",
            "2026-02-30",
            20261106,
            None,
        ):
            with self.subTest(value=value):
                self.assert_rejected(
                    dict(BASE_ARGS, incident_date=value), "incident_date"
                )

    def test_missing_incident_date_is_rejected(self) -> None:
        args = dict(BASE_ARGS)
        del args["incident_date"]
        self.assert_rejected(args, "incident_date")

    def test_wrongly_typed_identifiers_are_rejected(self) -> None:
        self.assert_rejected(dict(BASE_ARGS, claim_id=101), "claim_id")
        self.assert_rejected(dict(BASE_ARGS, policy_id=None), "policy_id")
        self.assert_rejected(dict(BASE_ARGS, claim_type="Collision With Spaces"), "claim_type")

    def test_arguments_that_are_not_an_object_are_rejected(self) -> None:
        for payload in ("claim_id=CLAIM-101", ["CLAIM-101"], 42, None):
            with self.subTest(payload=payload):
                self.assert_rejected(payload, "claim arguments")


@unittest.skipUnless(
    HOST_AVAILABLE,
    "requires the Hermes host API (plugins.plugin_storage)",
)
class ProseArtifactsAreBlockedAtTheGateTests(unittest.TestCase):
    """The gate blocks the same artifacts, in the one shape Hermes understands."""

    def setUp(self) -> None:
        self._old_home = os.environ.get("HERMES_HOME")
        self._temp = tempfile.TemporaryDirectory()
        os.environ["HERMES_HOME"] = self._temp.name
        conn = connect()
        seed_database(conn)
        conn.close()

    def tearDown(self) -> None:
        if self._old_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._old_home
        self._temp.cleanup()

    def assert_blocked(self, args: object) -> None:
        from claim_gate.hooks.execution_gate import pre_tool_call

        result = pre_tool_call("submit_claim_package", args)
        self.assertIsInstance(result, dict)
        assert isinstance(result, dict)
        self.assertEqual(set(result), {"action", "message"})
        self.assertEqual(result["action"], "block")
        self.assertTrue(result["message"].strip())

    def test_comma_separated_evidence_is_blocked(self) -> None:
        self.assert_blocked(dict(BASE_ARGS, evidence_ids="EV-001, EV-002, EV-003"))

    def test_human_readable_date_is_blocked(self) -> None:
        self.assert_blocked(dict(BASE_ARGS, incident_date="November 6, 2026"))

    def test_non_object_arguments_are_blocked(self) -> None:
        self.assert_blocked("claim_id=CLAIM-101")
        self.assert_blocked(["CLAIM-101"])

    def test_model_asserting_verification_does_not_help(self) -> None:
        """The prose says EV-005 is verified. The registry says pending."""

        from claim_gate.hooks.execution_gate import pre_tool_call

        result = pre_tool_call(
            "submit_claim_package",
            dict(
                BASE_ARGS,
                evidence_ids=["EV-005"],
                status="verified",
                verified=True,
                verified_by="adjuster@example",
                document_type="photo_evidence",
            ),
        )
        self.assertEqual(
            result,
            {"action": "block", "message": "ClaimGate: Unverified ID"},
        )


if __name__ == "__main__":
    unittest.main()
