"""Package fingerprinting, audit logging, review queue, and ledger access.

Fingerprint rule:
    SHA-256 over canonical JSON containing the normalized claim, the policy
    hash, and every resolved evidence/fact record (including per-record
    hashes). IDs are sorted and records are sorted, so submission order does
    not matter. Any changed registry value changes the fingerprint.

Human review rule:
    A release is valid only for the exact ``package_hash`` an operator
    reviewed. Resubmitting a changed package resets the queue entry to
    ``pending`` and invalidates the previous release.
"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..domain.claim import NormalizedClaim
from ..domain.policy import canonical_json_bytes
from .registry import (
    ConnectionFactory,
    EvidenceRecord,
    FactRecord,
    connect,
    initialize_schema,
)

__all__ = [
    "AuditRepository",
    "LedgerWriteResult",
    "compute_package_hash",
    "utc_now_iso",
]

PACKAGE_SCHEMA = "claimgate.package.v1"
AUDIT_DECISIONS = frozenset(
    {"PASS", "BLOCK", "HUMAN_REVIEW", "SUBMISSION_SUCCEEDED", "SUBMISSION_FAILED"}
)


def utc_now_iso() -> str:
    """Return a UTC timestamp in the schema's canonical text form."""

    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compute_package_hash(
    claim: NormalizedClaim,
    policy_hash: str,
    evidence: tuple[EvidenceRecord, ...] | list[EvidenceRecord],
    facts: tuple[FactRecord, ...] | list[FactRecord],
) -> str:
    """Compute the immutable SHA-256 fingerprint of a claim package.

    The fingerprint deliberately excludes wall-clock time, session IDs, and
    model prose. It is stable for identical claim + policy + registry state.
    """

    evidence_payload = []
    for record in sorted(evidence, key=lambda item: item.evidence_id):
        record_dict = record.to_dict()
        evidence_payload.append(
            record.to_dict(record_hash=_sha256_hex(canonical_json_bytes(record_dict)))
        )

    facts_payload = []
    for record in sorted(facts, key=lambda item: item.fact_id):
        record_dict = record.to_dict()
        facts_payload.append(
            record.to_dict(record_hash=_sha256_hex(canonical_json_bytes(record_dict)))
        )

    payload = {
        "schema": PACKAGE_SCHEMA,
        "claim": claim.to_dict(),
        "policy_hash": policy_hash,
        "evidence": evidence_payload,
        "facts": facts_payload,
    }
    return _sha256_hex(canonical_json_bytes(payload))


@dataclass(frozen=True, slots=True)
class LedgerWriteResult:
    inserted: bool
    existing_hash: str
    matches: bool


class AuditRepository:
    """SQLite adapter for audit, review, and ledger state."""

    def __init__(self, connection_factory: ConnectionFactory = connect) -> None:
        self._connection_factory = connection_factory

    def _connection(self) -> sqlite3.Connection:
        conn = self._connection_factory()
        conn.row_factory = sqlite3.Row
        initialize_schema(conn)
        return conn

    # ------------------------------------------------------------------
    # Audit log
    # ------------------------------------------------------------------

    def record_audit(
        self,
        tool_call_id: str,
        claim_id: str,
        decision: str,
        package_hash: str | None = None,
        timestamp: str | None = None,
    ) -> None:
        if decision not in AUDIT_DECISIONS:
            raise ValueError(f"unsupported audit decision: {decision!r}")
        if not tool_call_id or not tool_call_id.strip():
            raise ValueError("tool_call_id must be a non-empty string")
        if not claim_id:
            claim_id = "UNKNOWN"
        if package_hash is not None and len(package_hash) != 64:
            raise ValueError("package_hash must be a 64-character SHA-256 hex digest")

        with closing(self._connection()) as conn, conn:
            conn.execute(
                """
                INSERT INTO audit_log (tool_call_id, claim_id, decision, package_hash, timestamp)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(tool_call_id) DO UPDATE SET
                    claim_id = excluded.claim_id,
                    decision = excluded.decision,
                    package_hash = COALESCE(excluded.package_hash, audit_log.package_hash),
                    timestamp = excluded.timestamp
                """,
                (
                    tool_call_id.strip(),
                    claim_id.strip() or "UNKNOWN",
                    decision,
                    package_hash,
                    timestamp or utc_now_iso(),
                ),
            )

    def record_outcome(
        self,
        tool_call_id: str,
        claim_id: str,
        *,
        succeeded: bool,
        package_hash: str | None = None,
    ) -> None:
        self.record_audit(
            tool_call_id=tool_call_id,
            claim_id=claim_id,
            decision="SUBMISSION_SUCCEEDED" if succeeded else "SUBMISSION_FAILED",
            package_hash=package_hash,
        )

    def get_audit(self, claim_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
            raise ValueError("limit must be an integer between 1 and 200")
        with closing(self._connection()) as conn:
            rows = conn.execute(
                """
                SELECT tool_call_id, claim_id, decision, package_hash, timestamp
                FROM audit_log
                WHERE claim_id = ?
                ORDER BY timestamp DESC, tool_call_id DESC
                LIMIT ?
                """,
                (claim_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Human review queue
    # ------------------------------------------------------------------

    def queue_human_review(self, claim_id: str, package_hash: str) -> str:
        """Queue a package for operator review and return the resulting status.

        Same hash: preserve the current status (a prior release remains valid
        for that exact package). Different hash: reset to ``pending`` and
        clear the prior release timestamp.
        """

        if not claim_id or not claim_id.strip():
            raise ValueError("claim_id must be a non-empty string")
        if len(package_hash) != 64:
            raise ValueError("package_hash must be a 64-character SHA-256 hex digest")

        with closing(self._connection()) as conn, conn:
            conn.execute(
                """
                INSERT INTO human_review_queue (claim_id, package_hash, status)
                VALUES (?, ?, 'pending')
                ON CONFLICT(claim_id) DO UPDATE SET
                    package_hash = excluded.package_hash,
                    status = CASE
                        WHEN human_review_queue.package_hash = excluded.package_hash
                        THEN human_review_queue.status
                        ELSE 'pending'
                    END,
                    released_at = CASE
                        WHEN human_review_queue.package_hash = excluded.package_hash
                        THEN human_review_queue.released_at
                        ELSE NULL
                    END
                """,
                (claim_id.strip(), package_hash),
            )
            row = conn.execute(
                "SELECT status FROM human_review_queue WHERE claim_id = ?",
                (claim_id.strip(),),
            ).fetchone()
        return str(row["status"]) if row is not None else "pending"

    def release_human_review(self, claim_id: str, package_hash: str) -> bool:
        """Release exactly one reviewed package.

        Returns ``False`` for an unknown claim or a hash mismatch. Releasing
        an already-released identical hash is idempotent and returns ``True``.
        """

        if len(package_hash) != 64:
            return False
        with closing(self._connection()) as conn, conn:
            row = conn.execute(
                "SELECT package_hash, status FROM human_review_queue WHERE claim_id = ?",
                (claim_id,),
            ).fetchone()
            if row is None or row["package_hash"] != package_hash:
                return False
            if row["status"] == "released":
                return True
            if row["status"] != "pending":
                return False
            conn.execute(
                """
                UPDATE human_review_queue
                SET status = 'released', released_at = ?
                WHERE claim_id = ? AND package_hash = ? AND status = 'pending'
                """,
                (utc_now_iso(), claim_id, package_hash),
            )
        return True

    def has_release(self, claim_id: str, package_hash: str) -> bool:
        """Return whether this exact package hash has an operator release."""

        if len(package_hash) != 64:
            return False
        with closing(self._connection()) as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM human_review_queue
                WHERE claim_id = ? AND package_hash = ? AND status = 'released'
                """,
                (claim_id, package_hash),
            ).fetchone()
        return row is not None

    def get_review(self, claim_id: str) -> dict[str, Any] | None:
        with closing(self._connection()) as conn:
            row = conn.execute(
                """
                SELECT claim_id, package_hash, status, created_at, released_at
                FROM human_review_queue
                WHERE claim_id = ?
                """,
                (claim_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_reviews(self, *, status: str | None = "pending") -> list[dict[str, Any]]:
        if status is not None and status not in {"pending", "released"}:
            raise ValueError("status must be 'pending', 'released', or None")
        with closing(self._connection()) as conn:
            if status is None:
                rows = conn.execute(
                    """
                    SELECT claim_id, package_hash, status, created_at, released_at
                    FROM human_review_queue
                    ORDER BY created_at ASC, claim_id ASC
                    """
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT claim_id, package_hash, status, created_at, released_at
                    FROM human_review_queue
                    WHERE status = ?
                    ORDER BY created_at ASC, claim_id ASC
                    """,
                    (status,),
                ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Claim ledger
    # ------------------------------------------------------------------

    def record_ledger_entry(self, claim_id: str, package_hash: str) -> LedgerWriteResult:
        """Append a ledger row once; report whether the package matches.

        The ledger primary key enforces idempotency. If a claim already exists
        with a different hash, the caller must treat it as a conflict and
        block rather than overwrite history.
        """

        if not claim_id or not claim_id.strip():
            raise ValueError("claim_id must be a non-empty string")
        if len(package_hash) != 64:
            raise ValueError("package_hash must be a 64-character SHA-256 hex digest")

        claim_id = claim_id.strip()
        with closing(self._connection()) as conn, conn:
            existing = conn.execute(
                "SELECT package_hash FROM claim_ledger WHERE claim_id = ?",
                (claim_id,),
            ).fetchone()
            if existing is not None:
                existing_hash = str(existing["package_hash"])
                return LedgerWriteResult(
                    inserted=False,
                    existing_hash=existing_hash,
                    matches=existing_hash == package_hash,
                )

            conn.execute(
                "INSERT INTO claim_ledger (claim_id, package_hash, timestamp) VALUES (?, ?, ?)",
                (claim_id, package_hash, utc_now_iso()),
            )

        return LedgerWriteResult(inserted=True, existing_hash=package_hash, matches=True)

    def get_claim(self, claim_id: str) -> dict[str, Any] | None:
        with closing(self._connection()) as conn:
            row = conn.execute(
                "SELECT claim_id, package_hash, timestamp FROM claim_ledger WHERE claim_id = ?",
                (claim_id,),
            ).fetchone()
        return dict(row) if row is not None else None
