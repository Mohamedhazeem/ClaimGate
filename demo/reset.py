#!/usr/bin/env python3
"""Reset the ClaimGate demo fixture to a known-good state.

This is a **demo tool**, not part of the plugin runtime. It drops and
recreates the trusted SQLite tables, re-seeds the operator-controlled
registries, and restores the canonical policy file so a demo can be
rehearsed from scratch, every time.

Usage::

    python3 demo/reset.py            # reset the live Hermes home
    python3 demo/reset.py --temp     # reset a throwaway Hermes home instead

What it touches:

* ``<hermes home>/plugin-data/claim_gate/data.db`` -- the tables ClaimGate
  owns (``evidence_registry``, ``incident_fact_registry``, ``audit_log``,
  ``claim_ledger``, ``human_review_queue``). The database is opened through
  Hermes' sanctioned ``plugin_db()`` helper, never by a raw file path.
* ``policies/auto_policy.json`` -- the active policy, restored from the
  canonical template below and re-validated by the real policy loader.

The registry seed is the plugin's own seed set
(``services/registry.py``), so the demo can never drift from the runtime:

    EV-001  CLAIM-101  police_report    verified
    EV-002  CLAIM-101  photo_evidence   verified
    EV-003  CLAIM-101  repair_estimate  verified
    EV-004  CLAIM-102  police_report    verified
    EV-005  CLAIM-101  photo_evidence   pending    (not yet verified)
    FACT-001 CLAIM-101 activity=racing  verified
    FACT-002 CLAIM-101 activity=commuting verified
    FACT-003 CLAIM-101 activity=racing  pending    (not yet verified)

The two pending rows exist so the demo can show that an unverified
reference is blocked, exactly like a forged one.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from claim_gate.hosting import ensure_host_on_path  # noqa: E402

if not ensure_host_on_path():
    print(
        "error: the Hermes host API (plugins.plugin_storage) is not available.\n"
        "Run this script inside a Hermes install, or point HERMES_AGENT at a "
        "Hermes checkout."
    )
    raise SystemExit(2)

# The canonical demo policy. `tests/unit/test_demo_fixture.py` asserts that
# this template still matches `policies/auto_policy.json`, so the two can
# never drift apart.
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

# Every table ClaimGate owns. They are dropped and recreated together so a
# stale row from an earlier rehearsal can never survive into a demo.
MANAGED_TABLES = (
    "evidence_registry",
    "incident_fact_registry",
    "audit_log",
    "claim_ledger",
    "human_review_queue",
)

POLICY_PATH = ROOT / "policies" / "auto_policy.json"


def reset_policy(path: Path = POLICY_PATH) -> str:
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


def reset_database() -> None:
    """Drop, recreate, and re-seed every ClaimGate table."""

    from claim_gate.services import registry

    conn = registry.connect()
    try:
        for table in MANAGED_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.commit()

        # Recreate from the canonical schema. `initialize_schema` is skipped
        # here on purpose: its per-process cache would skip the DDL, and the
        # tables were just dropped.
        conn.executescript(registry.SCHEMA_PATH.read_text(encoding="utf-8"))
        registry.seed_database(conn)
        conn.commit()
    finally:
        conn.close()


def _print_summary() -> None:
    from claim_gate.services import registry

    conn = registry.connect()
    try:
        conn.row_factory = None
        print("evidence_registry")
        for row in conn.execute(
            "SELECT evidence_id, claim_id, document_type, status "
            "FROM evidence_registry ORDER BY evidence_id"
        ):
            print(f"  {row[0]:<10} {row[1]:<12} {row[2]:<18} {row[3]}")

        print("incident_fact_registry")
        for row in conn.execute(
            "SELECT fact_id, claim_id, fact_type, fact_value, status "
            "FROM incident_fact_registry ORDER BY fact_id"
        ):
            print(f"  {row[0]:<10} {row[1]:<12} {row[2]:<10} {row[3]:<12} {row[4]}")

        counts = {}
        for table in ("audit_log", "claim_ledger", "human_review_queue"):
            counts[table] = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        print("operational state (all empty after a reset)")
        for table, count in counts.items():
            print(f"  {table:<20} {count} row(s)")
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--temp",
        action="store_true",
        help="Use a throwaway Hermes home instead of the live one.",
    )
    args = parser.parse_args()

    temp_home = None
    if args.temp:
        temp_home = tempfile.TemporaryDirectory()
        os.environ["HERMES_HOME"] = temp_home.name

    try:
        print("ClaimGate demo reset")
        print(f"  hermes home : {os.environ.get('HERMES_HOME', '<default>')}")
        policy_hash = reset_policy()
        print(f"  policy      : {POLICY_PATH.name} ({policy_hash[:16]}...)")
        reset_database()
        print("  tables      : dropped and recreated")
        print()
        _print_summary()
        print()
        print("reset complete. The demo can start from a clean state.")
        return 0
    finally:
        if temp_home is not None:
            temp_home.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
