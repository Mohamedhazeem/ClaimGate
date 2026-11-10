"""Package fingerprinting, audit logging, review queue, and ledger access.

Fingerprint rule:
    SHA-256 over canonical JSON containing the normalized claim, the policy
    hash, and every resolved evidence/fact record (including per-record
    hashes). IDs are sorted and records are sorted, so submission order does
    not matter. Any changed registry value changes the fingerprint.

    ``generate_package_hash`` is the single constructor of that fingerprint.
    Called with the claim inputs alone it binds the claim, the policy, and
    the referenced IDs. Called with the resolved registry rows (the
    enforcement path) it also binds the *content* of those rows, so an
    operator release is invalidated by any registry change -- not only by a
    change of the claim itself.

Human review rule:
    A release is valid only for the exact ``package_hash`` an operator
    reviewed. Resubmitting a changed package resets the queue entry to
    ``pending`` and invalidates the previous release.

Ledger rule:
    ``claim_ledger`` is append-only and keyed by ``claim_id``. A second
    submission of the same claim never creates a second row; the existing
    row is returned instead.
"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

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
    "generate_package_hash",
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


def _record_digest(payload: dict[str, Any]) -> str:
    return _sha256_hex(canonical_json_bytes(payload))


def generate_package_hash(
    claim_id: str,
    policy_hash: str,
    incident_date: str,
    claim_type: str,
    evidence_ids: Iterable[str],
    incident_fact_ids: Iterable[str],
    *,
    evidence_records: Sequence[EvidenceRecord] | None = None,
    fact_records: Sequence[FactRecord] | None = None,
) -> str:
    """Return the immutable SHA-256 fingerprint of a claim package.

    The canonical payload holds the claim identity, the policy hash, the
    sorted evidence/fact references, and -- when the resolved registry rows
    are supplied -- the digest of each resolved row. The fingerprint
    deliberately excludes wall-clock time, session IDs, and model prose, so
    identical inputs always produce an identical hash.

    Supplying ``evidence_records``/``fact_records`` is what binds the
    fingerprint to registry *content*: changing an evidence document type,
    its verification status, or a fact value changes the hash and therefore
    invalidates an operator release.
    """

    payload: dict[str, Any] = {
        "schema": PACKAGE_SCHEMA,
        "claim_id": claim_id,
        "policy_hash": policy_hash,
        "incident_date": incident_date,
        "claim_type": claim_type,
        "evidence_ids": sorted(set(evidence_ids)),
        "incident_fact_ids": sorted(set(incident_fact_ids)),
    }

    resolved: list[str] = []
    for record in sorted(evidence_records or (), key=lambda item: item.evidence_id):
        resolved.append(
            f"evidence:{record.evidence_id}:{_record_digest(record.to_dict())}"
        )
    for record in sorted(fact_records or (), key=lambda item: item.fact_id):
        resolved.append(f"fact:{record.fact_id}:{_record_digest(record.to_dict())}")
    if resolved:
        payload["resolved"] = sorted(resolved)

    return _sha256_hex(canonical_json_bytes(payload))


def compute_package_hash(
    claim: NormalizedClaim,
    policy_hash: str,
    evidence: tuple[EvidenceRecord, ...] | list[EvidenceRecord],
    facts: tuple[FactRecord, ...] | list[FactRecord],
) -> str:
    """Compute the immutable SHA-256 fingerprint of a claim package.

    Thin wrapper over :func:`generate_package_hash` that binds the resolved
    registry rows as well as the claim inputs.
    """

    return generate_package_hash(
        claim.claim_id,
        policy_hash,
        claim.incident_date.isoformat(),
        claim.claim_type,
        claim.evidence_ids,
        claim.incident_fact_ids,
        evidence_records=evidence,
        fact_records=facts,
    )


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
        *,
        execution_success: bool | None = None,
        hermes_tool_call_id: str | None = None,
    ) -> None:
        """Write or update the audit row for one tool call.

        The row is keyed by ``tool_call_id``. Re-writing the same key updates
        the decision and never appends a second row, so one submission is
        one audit row.
        """

        if decision not in AUDIT_DECISIONS:
            raise ValueError(f"unsupported audit decision: {decision!r}")
        if not tool_call_id or not tool_call_id.strip():
            raise ValueError("tool_call_id must be a non-empty string")
        if not claim_id:
            claim_id = "UNKNOWN"
        if package_hash is not None and len(package_hash) != 64:
            raise ValueError("package_hash must be a 64-character SHA-256 hex digest")
        if execution_success is not None and not isinstance(execution_success, bool):
            raise ValueError("execution_success must be a boolean or None")

        with closing(self._connection()) as conn, conn:
            conn.execute(
                """
                INSERT INTO audit_log (
                    tool_call_id, hermes_tool_call_id, claim_id, decision,
                    package_hash, execution_success, timestamp
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tool_call_id) DO UPDATE SET
                    hermes_tool_call_id = COALESCE(
                        excluded.hermes_tool_call_id, audit_log.hermes_tool_call_id
                    ),
                    claim_id = excluded.claim_id,
                    decision = excluded.decision,
                    package_hash = COALESCE(excluded.package_hash, audit_log.package_hash),
                    execution_success = COALESCE(
                        excluded.execution_success, audit_log.execution_success
                    ),
                    timestamp = excluded.timestamp
                """,
                (
                    tool_call_id.strip(),
                    hermes_tool_call_id.strip() if hermes_tool_call_id else None,
                    claim_id.strip() or "UNKNOWN",
                    decision,
                    package_hash,
                    None if execution_success is None else int(execution_success),
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
        timestamp: str | None = None,
    ) -> None:
        """Record the final outcome of a protected submission.

        The policy decision recorded by the gate is left untouched; only the
        execution outcome is added, so the audit row keeps both facts.
        """

        self.record_execution(
            tool_call_id,
            claim_id,
            succeeded=succeeded,
            package_hash=package_hash,
            timestamp=timestamp,
        )

    def record_execution(
        self,
        tool_call_id: str,
        claim_id: str,
        *,
        succeeded: bool,
        package_hash: str | None = None,
        timestamp: str | None = None,
    ) -> None:
        """Update the execution outcome of an existing audit row.

        The row is created when it does not exist yet, so a submission that
        never reached the gate is still recorded.
        """

        if not isinstance(succeeded, bool):
            raise ValueError("succeeded must be a boolean")
        if package_hash is not None and len(package_hash) != 64:
            raise ValueError("package_hash must be a 64-character SHA-256 hex digest")

        with closing(self._connection()) as conn, conn:
            conn.execute(
                """
                INSERT INTO audit_log (
                    tool_call_id, claim_id, decision, package_hash,
                    execution_success, timestamp
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(tool_call_id) DO UPDATE SET
                    claim_id = excluded.claim_id,
                    package_hash = COALESCE(excluded.package_hash, audit_log.package_hash),
                    execution_success = excluded.execution_success,
                    timestamp = excluded.timestamp
                """,
                (
                    tool_call_id.strip(),
                    claim_id.strip() or "UNKNOWN",
                    "PASS",
                    package_hash,
                    int(succeeded),
                    timestamp or utc_now_iso(),
                ),
            )

    def get_audit(self, claim_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
            raise ValueError("limit must be an integer between 1 and 200")
        with closing(self._connection()) as conn:
            rows = conn.execute(
                """
                SELECT tool_call_id, hermes_tool_call_id, claim_id, decision,
                       package_hash, execution_success, timestamp
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

    def queue_human_review(
        self,
        claim_id: str,
        package_hash: str,
        reason: str | None = None,
    ) -> str:
        """Queue a package for operator review and return the resulting status.

        Same hash: preserve the current status (a prior release remains valid
        for that exact package). Different hash: reset to ``pending`` and
        clear the prior release timestamp, because the operator reviewed a
        different package.
        """

        if not claim_id or not claim_id.strip():
            raise ValueError("claim_id must be a non-empty string")
        if len(package_hash) != 64:
            raise ValueError("package_hash must be a 64-character SHA-256 hex digest")

        with closing(self._connection()) as conn, conn:
            conn.execute(
                """
                INSERT INTO human_review_queue (claim_id, package_hash, status, reason)
                VALUES (?, ?, 'pending', ?)
                ON CONFLICT(claim_id) DO UPDATE SET
                    package_hash = excluded.package_hash,
                    status = CASE
                        WHEN human_review_queue.package_hash = excluded.package_hash
                        THEN human_review_queue.status
                        ELSE 'pending'
                    END,
                    reason = excluded.reason,
                    released_at = CASE
                        WHEN human_review_queue.package_hash = excluded.package_hash
                        THEN human_review_queue.released_at
                        ELSE NULL
                    END
                """,
                (claim_id.strip(), package_hash, reason),
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
                """
                SELECT package_hash, status FROM human_review_queue WHERE claim_id = ?
                """,
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
                SELECT claim_id, package_hash, status, reason, created_at, released_at
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
                    SELECT claim_id, package_hash, status, reason, created_at, released_at
                    FROM human_review_queue
                    ORDER BY created_at ASC, claim_id ASC
                    """
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT claim_id, package_hash, status, reason, created_at, released_at
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

            now = utc_now_iso()
            conn.execute(
                """
                INSERT INTO claim_ledger (claim_id, package_hash, submitted_at, timestamp)
                VALUES (?, ?, ?, ?)
                """,
                (claim_id, package_hash, now, now),
            )

        return LedgerWriteResult(inserted=True, existing_hash=package_hash, matches=True)

    def get_claim(self, claim_id: str) -> dict[str, Any] | None:
        with closing(self._connection()) as conn:
            row = conn.execute(
                """
                SELECT claim_id, package_hash,
                       COALESCE(submitted_at, timestamp) AS submitted_at
                FROM claim_ledger WHERE claim_id = ?
                """,
                (claim_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_claims(self) -> list[dict[str, Any]]:
        """Return every ledger row, oldest first (read-only operator view)."""

        with closing(self._connection()) as conn:
            rows = conn.execute(
                """
                SELECT claim_id, package_hash,
                       COALESCE(submitted_at, timestamp) AS submitted_at
                FROM claim_ledger
                ORDER BY submitted_at ASC, claim_id ASC
                """
            ).fetchall()
        return [dict(row) for row in rows]
