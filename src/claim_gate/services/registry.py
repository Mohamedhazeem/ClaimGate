"""Trusted evidence and incident-fact registry adapters.

The registries are the security boundary. The model supplies only IDs; this
module resolves those IDs against operator-controlled SQLite rows and rejects
missing, cross-claim, or non-verified records.

Connections are obtained exclusively through Hermes' ``plugin_db()`` helper in
production. A connection factory is injectable for tests and keeps the
adapter independent of Hermes' import path at module import time.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

__all__ = [
    "EvidenceRecord",
    "EvidenceRegistry",
    "FactRecord",
    "RegistryViolation",
    "SCHEMA_PATH",
    "connect",
    "initialize_schema",
]

PLUGIN_ID = "claim-gate"
SCHEMA_PATH = Path(__file__).with_name("schema.sql")

_INITIALIZED_DATABASES: set[str] = set()
_INIT_LOCK = threading.Lock()


class RegistryViolation(Exception):
    """A trusted-registry lookup failed a hard security invariant.

    ``code`` is stable for audit/tooling; ``record_id`` is the offending ID.
    The message is safe to surface to the model because it contains only the
    ID and a generic reason.
    """

    def __init__(self, code: str, record_id: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.record_id = record_id


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    evidence_id: str
    claim_id: str
    document_type: str
    status: str
    verified_by: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "EvidenceRecord":
        return cls(
            evidence_id=row["evidence_id"],
            claim_id=row["claim_id"],
            document_type=row["document_type"],
            status=row["status"],
            verified_by=row["verified_by"],
        )

    def to_dict(self, *, record_hash: str | None = None) -> dict[str, object]:
        payload: dict[str, object] = {
            "evidence_id": self.evidence_id,
            "claim_id": self.claim_id,
            "document_type": self.document_type,
            "status": self.status,
            "verified_by": self.verified_by,
        }
        if record_hash is not None:
            payload["record_hash"] = record_hash
        return payload


@dataclass(frozen=True, slots=True)
class FactRecord:
    fact_id: str
    claim_id: str
    fact_type: str
    fact_value: str
    status: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "FactRecord":
        return cls(
            fact_id=row["fact_id"],
            claim_id=row["claim_id"],
            fact_type=row["fact_type"],
            fact_value=row["fact_value"],
            status=row["status"],
        )

    def to_dict(self, *, record_hash: str | None = None) -> dict[str, object]:
        payload: dict[str, object] = {
            "fact_id": self.fact_id,
            "claim_id": self.claim_id,
            "fact_type": self.fact_type,
            "fact_value": self.fact_value,
            "status": self.status,
        }
        if record_hash is not None:
            payload["record_hash"] = record_hash
        return payload


def connect() -> sqlite3.Connection:
    """Open the plugin database through Hermes' sanctioned storage helper.

    ``plugin_db`` selects the active profile, creates the data directory,
    enables WAL where supported, and enables foreign keys. ClaimGate never
    opens an arbitrary database path.
    """

    from plugins.plugin_storage import plugin_db  # local import: host-provided

    conn = plugin_db(PLUGIN_ID)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _database_key(conn: sqlite3.Connection) -> str:
    row = conn.execute("PRAGMA database_list").fetchone()
    if row is None:
        return ""
    # sqlite3.Row supports index access; plain tuples do too.
    return str(row[2] or "")


def initialize_schema(conn: sqlite3.Connection) -> None:
    """Apply ``schema.sql`` once per on-disk database per process.

    The DDL is idempotent. The in-process set is only an optimization; if the
    database is recreated underneath the plugin, the next connection still
    works because ``connect()`` does not cache connections.
    """

    key = _database_key(conn)
    if key:
        with _INIT_LOCK:
            if key in _INITIALIZED_DATABASES:
                return
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(sql)
    if key:
        with _INIT_LOCK:
            _INITIALIZED_DATABASES.add(key)


ConnectionFactory = Callable[[], sqlite3.Connection]


def _require_text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _dedupe(ids: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(ids))


class EvidenceRegistry:
    """Read-only resolver over the operator-controlled trust anchors."""

    def __init__(self, connection_factory: ConnectionFactory = connect) -> None:
        self._connection_factory = connection_factory

    def _connection(self) -> sqlite3.Connection:
        conn = self._connection_factory()
        conn.row_factory = sqlite3.Row
        initialize_schema(conn)
        return conn

    def resolve_evidence_ids(
        self, claim_id: str, evidence_ids: tuple[str, ...] | list[str]
    ) -> tuple[EvidenceRecord, ...]:
        """Resolve evidence IDs, enforcing existence, claim ownership, status.

        Raises :class:`RegistryViolation` for the first violation in the
        supplied order. Empty input is valid at this layer; policy (not the
        registry) decides whether a required document is missing.
        """

        trusted_claim_id = _require_text(claim_id, field="claim_id")
        requested = _dedupe(tuple(_require_text(i, field="evidence_id") for i in evidence_ids))
        if not requested:
            return ()

        with closing(self._connection()) as conn:
            placeholders = ",".join("?" for _ in requested)
            rows = conn.execute(
                "SELECT evidence_id, claim_id, document_type, status, verified_by "
                f"FROM evidence_registry WHERE evidence_id IN ({placeholders})",
                requested,
            ).fetchall()

        by_id = {row["evidence_id"]: EvidenceRecord.from_row(row) for row in rows}
        resolved: list[EvidenceRecord] = []
        for evidence_id in requested:
            record = by_id.get(evidence_id)
            if record is None:
                raise RegistryViolation(
                    "EVIDENCE_NOT_FOUND",
                    evidence_id,
                    f"Evidence ID {evidence_id!r} is not present in the trusted registry.",
                )
            if record.claim_id != trusted_claim_id:
                raise RegistryViolation(
                    "EVIDENCE_CLAIM_MISMATCH",
                    evidence_id,
                    f"Evidence ID {evidence_id!r} does not belong to claim {trusted_claim_id!r}.",
                )
            if record.status != "verified":
                raise RegistryViolation(
                    "EVIDENCE_NOT_VERIFIED",
                    evidence_id,
                    f"Evidence ID {evidence_id!r} is not verified in the trusted registry.",
                )
            resolved.append(record)
        return tuple(resolved)

    def resolve_fact_ids(
        self, claim_id: str, fact_ids: tuple[str, ...] | list[str]
    ) -> tuple[FactRecord, ...]:
        """Resolve incident-fact IDs with the same trust invariants."""

        trusted_claim_id = _require_text(claim_id, field="claim_id")
        requested = _dedupe(tuple(_require_text(i, field="fact_id") for i in fact_ids))
        if not requested:
            return ()

        with closing(self._connection()) as conn:
            placeholders = ",".join("?" for _ in requested)
            rows = conn.execute(
                "SELECT fact_id, claim_id, fact_type, fact_value, status "
                f"FROM incident_fact_registry WHERE fact_id IN ({placeholders})",
                requested,
            ).fetchall()

        by_id = {row["fact_id"]: FactRecord.from_row(row) for row in rows}
        resolved: list[FactRecord] = []
        for fact_id in requested:
            record = by_id.get(fact_id)
            if record is None:
                raise RegistryViolation(
                    "FACT_NOT_FOUND",
                    fact_id,
                    f"Fact ID {fact_id!r} is not present in the trusted registry.",
                )
            if record.claim_id != trusted_claim_id:
                raise RegistryViolation(
                    "FACT_CLAIM_MISMATCH",
                    fact_id,
                    f"Fact ID {fact_id!r} does not belong to claim {trusted_claim_id!r}.",
                )
            if record.status != "verified":
                raise RegistryViolation(
                    "FACT_NOT_VERIFIED",
                    fact_id,
                    f"Fact ID {fact_id!r} is not verified in the trusted registry.",
                )
            resolved.append(record)
        return tuple(resolved)
