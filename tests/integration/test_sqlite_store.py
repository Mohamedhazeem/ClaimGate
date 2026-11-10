from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from claim_gate.services.audit import AuditRepository  # noqa: E402
from claim_gate.services.registry import (  # noqa: E402
    EvidenceRegistry,
    RegistryViolation,
    initialize_schema,
)

HASH_A = "a" * 64
HASH_B = "b" * 64


class SqliteStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "claimgate.db"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def factory(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def seed(self) -> None:
        with closing(self.factory()) as conn:
            initialize_schema(conn)
            conn.executemany(
                """
                INSERT INTO evidence_registry
                    (evidence_id, claim_id, document_type, status, verified_by)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    ("EV-001", "CLAIM-101", "police_report", "verified", "operator-1"),
                    ("EV-002", "CLAIM-101", "repair_estimate", "verified", "operator-1"),
                    ("EV-003", "CLAIM-101", "photos", "pending", None),
                    ("EV-010", "CLAIM-102", "police_report", "verified", "operator-1"),
                ],
            )
            conn.executemany(
                """
                INSERT INTO incident_fact_registry
                    (fact_id, claim_id, fact_type, fact_value, status)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    ("FACT-001", "CLAIM-101", "activity", "commuting", "verified"),
                    ("FACT-002", "CLAIM-101", "activity", "racing", "pending"),
                    ("FACT-010", "CLAIM-102", "activity", "commuting", "verified"),
                ],
            )
            conn.commit()

    def test_schema_tables_exist(self) -> None:
        with closing(self.factory()) as conn:
            initialize_schema(conn)
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertTrue(
            {
                "evidence_registry",
                "incident_fact_registry",
                "audit_log",
                "claim_ledger",
                "human_review_queue",
            }.issubset(tables)
        )

    def test_resolves_verified_evidence(self) -> None:
        self.seed()
        registry = EvidenceRegistry(self.factory)
        records = registry.resolve_evidence_ids("CLAIM-101", ["EV-001", "EV-002"])
        self.assertEqual([record.evidence_id for record in records], ["EV-001", "EV-002"])

    def test_rejects_forged_evidence(self) -> None:
        self.seed()
        registry = EvidenceRegistry(self.factory)
        with self.assertRaises(RegistryViolation) as raised:
            registry.resolve_evidence_ids("CLAIM-101", ["EV-999"])
        self.assertEqual(raised.exception.code, "EVIDENCE_NOT_FOUND")

    def test_rejects_cross_claim_evidence(self) -> None:
        self.seed()
        registry = EvidenceRegistry(self.factory)
        with self.assertRaises(RegistryViolation) as raised:
            registry.resolve_evidence_ids("CLAIM-102", ["EV-001"])
        self.assertEqual(raised.exception.code, "EVIDENCE_CLAIM_MISMATCH")

    def test_rejects_unverified_evidence(self) -> None:
        self.seed()
        registry = EvidenceRegistry(self.factory)
        with self.assertRaises(RegistryViolation) as raised:
            registry.resolve_evidence_ids("CLAIM-101", ["EV-003"])
        self.assertEqual(raised.exception.code, "EVIDENCE_NOT_VERIFIED")

    def test_resolves_facts_and_rejects_cross_claim_fact(self) -> None:
        self.seed()
        registry = EvidenceRegistry(self.factory)
        records = registry.resolve_fact_ids("CLAIM-101", ["FACT-001"])
        self.assertEqual(records[0].fact_value, "commuting")

        with self.assertRaises(RegistryViolation) as raised:
            registry.resolve_fact_ids("CLAIM-101", ["FACT-010"])
        self.assertEqual(raised.exception.code, "FACT_CLAIM_MISMATCH")

    def test_review_release_is_bound_to_exact_hash(self) -> None:
        self.seed()
        repository = AuditRepository(self.factory)
        self.assertEqual(repository.queue_human_review("CLAIM-101", HASH_A), "pending")
        self.assertFalse(repository.has_release("CLAIM-101", HASH_A))
        self.assertFalse(repository.release_human_review("CLAIM-101", HASH_B))
        self.assertTrue(repository.release_human_review("CLAIM-101", HASH_A))
        self.assertTrue(repository.has_release("CLAIM-101", HASH_A))

        # Re-queueing the same package preserves the operator release.
        self.assertEqual(repository.queue_human_review("CLAIM-101", HASH_A), "released")
        self.assertTrue(repository.has_release("CLAIM-101", HASH_A))

        # A changed package invalidates the old release and resets the queue.
        self.assertEqual(repository.queue_human_review("CLAIM-101", HASH_B), "pending")
        self.assertFalse(repository.has_release("CLAIM-101", HASH_A))
        self.assertFalse(repository.has_release("CLAIM-101", HASH_B))
        review = repository.get_review("CLAIM-101")
        self.assertIsNotNone(review)
        assert review is not None
        self.assertIsNone(review["released_at"])

    def test_ledger_is_idempotent_and_append_only(self) -> None:
        self.seed()
        repository = AuditRepository(self.factory)
        first = repository.record_ledger_entry("CLAIM-101", HASH_A)
        self.assertTrue(first.inserted)
        self.assertTrue(first.matches)

        second = repository.record_ledger_entry("CLAIM-101", HASH_A)
        self.assertFalse(second.inserted)
        self.assertTrue(second.matches)

        conflict = repository.record_ledger_entry("CLAIM-101", HASH_B)
        self.assertFalse(conflict.inserted)
        self.assertFalse(conflict.matches)

        with closing(self.factory()) as conn:
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute(
                    "UPDATE claim_ledger SET package_hash = ? WHERE claim_id = ?",
                    (HASH_B, "CLAIM-101"),
                )

    def test_audit_decision_and_outcome(self) -> None:
        self.seed()
        repository = AuditRepository(self.factory)
        repository.record_audit("call-1", "CLAIM-101", "PASS", HASH_A)
        rows = repository.get_audit("CLAIM-101")
        self.assertEqual(rows[0]["decision"], "PASS")

        repository.record_outcome(
            "call-1", "CLAIM-101", succeeded=True, package_hash=HASH_A
        )
        rows = repository.get_audit("CLAIM-101")
        self.assertEqual(rows[0]["decision"], "SUBMISSION_SUCCEEDED")
        self.assertEqual(rows[0]["package_hash"], HASH_A)


if __name__ == "__main__":
    unittest.main()
