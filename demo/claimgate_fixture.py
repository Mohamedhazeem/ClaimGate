"""Shared fixture logic for the ClaimGate demo scripts.

`demo/reset.py` and `demo/run_demo.py` both need the same three things: the
canonical demo policy, a way to rebuild the ClaimGate tables, and a way to
clear the operational state between runs. They live here so the two scripts
cannot drift apart.

This module is demo material. It is not runtime code and is never reachable
from a tool or a hook.
"""

from __future__ import annotations

import json
from pathlib import Path

__all__ = [
    "MANAGED_TABLES",
    "OPERATIONAL_TABLES",
    "POLICY_TEMPLATE",
    "reset_database",
    "reset_operational_state",
    "reset_policy",
]

#: The canonical demo policy. `tests/unit/test_demo_fixture.py` asserts that
#: this template still matches `policies/auto_policy.json`, so the two can
#: never drift apart.
POLICY_TEMPLATE: dict[str, object] = {
    "policy_id": "AUTO-001",
    "policy_term": {
        "effective_from": "2026-01-01",
        "effective_through": "2026-12-31",
    },
    "coverages": {
        "collision": {
            "enabled": True,
            "required_documents": [
                "police_report",
                "repair_estimate",
                "photo_evidence",
            ],
            "required_verified_facts": [
                "activity",
            ],
            "review_triggers": [
                {
                    "fact_type": "activity",
                    "values": [
                        "racing",
                    ],
                },
            ],
            "waiting_period": {
                "coverage_available_on": "2026-01-31",
            },
        }
    },
}

#: Every table ClaimGate owns.
MANAGED_TABLES = (
    "evidence_registry",
    "incident_fact_registry",
    "audit_log",
    "claim_ledger",
    "human_review_queue",
)

#: The tables that record what happened, as opposed to the operator's
#: trusted registries. These are cleared between demo runs so the storyboard
#: always starts from the same state.
OPERATIONAL_TABLES = (
    "audit_log",
    "claim_ledger",
    "human_review_queue",
)


def reset_policy(path: Path) -> str:
    """Restore the canonical policy file and validate it with the real loader."""

    from claim_gate.domain.policy import load_policy

    if path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            current = None
        if current is not None and current != POLICY_TEMPLATE:
            print(
                f"  note: {path.name} differed from the canonical demo policy "
                "and was restored"
            )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(POLICY_TEMPLATE, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    policy = load_policy(path)
    return policy.source_hash


def _recreate(conn, tables: tuple[str, ...]) -> None:
    """Drop the given tables and recreate them from the canonical schema."""

    from claim_gate.services import registry

    for table in tables:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.commit()

    # Recreate from the canonical schema. `initialize_schema` is skipped here
    # on purpose: its per-process cache would skip the DDL, and the tables
    # were just dropped.
    conn.executescript(registry.SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()


def reset_database() -> None:
    """Drop, recreate, and re-seed every ClaimGate table."""

    from claim_gate.services import registry

    conn = registry.connect()
    try:
        _recreate(conn, MANAGED_TABLES)
        registry.seed_database(conn)
        conn.commit()
    finally:
        conn.close()


def reset_operational_state() -> None:
    """Clear the audit trail, the ledger, and the review queue.

    The operator's trusted registries are left in place: the storyboard needs
    them, and the operator's rows are not the demo's to throw away.
    """

    from claim_gate.services import registry

    conn = registry.connect()
    try:
        _recreate(conn, OPERATIONAL_TABLES)
    finally:
        conn.close()
