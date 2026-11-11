#!/usr/bin/env python3
"""Reset the ClaimGate demo fixture to a known-good state.

This is a **demo tool**, not part of the plugin runtime. It drops and
recreates the trusted SQLite tables, re-seeds the operator-controlled
registries, and restores the canonical policy file so a demo can be
rehearsed from scratch, every time.

Usage::

    python3 demo/reset.py            # a throwaway Hermes home (default)
    python3 demo/reset.py --live     # the REAL Hermes home (destroys state)

**The default is safe.** ``reset.py`` drops every ClaimGate table, including
the operator's trusted registries, so it refuses to touch a real installation
unless ``--live`` is passed explicitly. ``demo/run_demo.py`` uses the same
home by default, so the usual workflow is::

    python3 demo/reset.py
    python3 demo/run_demo.py

Both resolve the same shared throwaway home. Point it somewhere else with
``CLAIM_GATE_DEMO_HOME``.

What it touches:

* ``<hermes home>/plugin-data/claim-gate/data.db`` -- the tables ClaimGate
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
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from claimgate_fixture import (  # noqa: E402
    MANAGED_TABLES,
    OPERATIONAL_TABLES,
    POLICY_TEMPLATE,
    reset_database,
    reset_operational_state,
    reset_policy,
)
from claim_gate.hosting import demo_home, ensure_host_on_path  # noqa: E402

if not ensure_host_on_path():
    print(
        "error: the Hermes host API (plugins.plugin_storage) is not available.\n"
        "Run this script inside a Hermes install, or point HERMES_AGENT at a "
        "Hermes checkout."
    )
    raise SystemExit(2)

POLICY_PATH = ROOT / "policies" / "auto_policy.json"

__all__ = [
    "MANAGED_TABLES",
    "OPERATIONAL_TABLES",
    "POLICY_PATH",
    "POLICY_TEMPLATE",
    "reset_database",
    "reset_operational_state",
    "reset_policy",
]


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
            print(f"  {row[0]:<10} {row[1]:<12} {row[2]:<10} {row[3]:<18} {row[4]}")

        counts = {}
        for table in OPERATIONAL_TABLES:
            counts[table] = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        print("operational state (all empty after a reset)")
        for table, count in counts.items():
            print(f"  {table:<20} {count} row(s)")
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--live",
        action="store_true",
        help=(
            "Reset the REAL Hermes home. This drops every ClaimGate table, "
            "including the operator's trusted registries."
        ),
    )
    group.add_argument(
        "--temp",
        action="store_true",
        help="Use the shared throwaway Hermes home (default).",
    )
    args = parser.parse_args()

    home = demo_home(live=args.live)
    os.environ["HERMES_HOME"] = str(home)

    print("ClaimGate demo reset")
    print(f"  hermes home : {home}")
    if args.live:
        if home == Path.home() / ".hermes":
            print(
                "  WARNING      : this is the REAL Hermes home. Every ClaimGate "
                "table is dropped,\n"
                "                 including the operator's trusted registries."
            )
        else:
            print(
                "  note         : HERMES_HOME is set, so that home is used "
                "as-is. Every ClaimGate\n"
                "                 table in it is dropped, including the trusted "
                "registries."
            )
    policy_hash = reset_policy(POLICY_PATH)
    print(f"  policy      : {POLICY_PATH.name} ({policy_hash[:16]}...)")
    reset_database()
    print("  tables      : dropped and recreated")
    print()
    _print_summary()
    print()
    print("reset complete. The demo can start from a clean state.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
