"""Operator CLI: ``hermes claimgate review-list`` and ``review-release``.

This module is registered through ``ctx.register_cli_command()``. It is never
exposed as a model-facing tool, so the agent cannot approve or release its own
claim. A release is accepted only for the exact package hash in the queue.
"""

from __future__ import annotations

import argparse
import json
import re

from ..services.audit import AuditRepository

__all__ = ["handle_command", "setup_argparse"]

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def setup_argparse(subparser: argparse.ArgumentParser) -> None:
    """Build the ``hermes claimgate`` argparse tree."""

    subparsers = subparser.add_subparsers(
        dest="claimgate_command",
        metavar="{review-list,review-release}",
    )

    review_list = subparsers.add_parser(
        "review-list",
        help="List claims awaiting human review",
        description="List ClaimGate's human-review queue. Defaults to pending rows.",
    )
    review_list.add_argument(
        "--all",
        action="store_true",
        help="Include released review rows as well as pending rows.",
    )
    review_list.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of a table.",
    )

    release = subparsers.add_parser(
        "review-release",
        help="Release one exact reviewed package hash",
        description=(
            "Mark a queued package as operator-released. The claim_id and "
            "--package-hash must match the queue entry exactly."
        ),
    )
    release.add_argument("claim_id", help="Claim identifier to release")
    release.add_argument(
        "--package-hash",
        required=True,
        dest="package_hash",
        help="Exact 64-character SHA-256 package hash shown by review-list",
    )

    subparser.set_defaults(func=handle_command)


def _print_json(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _review_list(args: argparse.Namespace) -> int:
    rows = AuditRepository().list_reviews(status=None if args.all else "pending")
    if args.json:
        _print_json({"ok": True, "count": len(rows), "reviews": rows})
        return 0

    if not rows:
        print("No review rows.")
        return 0

    print(f"{'CLAIM':<20} {'STATUS':<10} {'CREATED':<22} PACKAGE_HASH")
    for row in rows:
        print(
            f"{row['claim_id']:<20} {row['status']:<10} "
            f"{row['created_at']:<22} {row['package_hash']}"
        )
    return 0


def _review_release(args: argparse.Namespace) -> int:
    claim_id = args.claim_id.strip()
    package_hash = args.package_hash.strip().lower()
    if not claim_id:
        print("error: claim_id must not be empty")
        return 2
    if not _HASH_RE.fullmatch(package_hash):
        print("error: --package-hash must be a 64-character lowercase SHA-256 hex digest")
        return 2

    repository = AuditRepository()
    review = repository.get_review(claim_id)
    if review is None:
        print(f"error: no review row exists for {claim_id}")
        return 1
    if review["package_hash"] != package_hash:
        print(
            "error: package hash mismatch; the claim changed after review and "
            "must be reviewed again"
        )
        print(f"expected: {review['package_hash']}")
        print(f"received: {package_hash}")
        return 1

    if not repository.release_human_review(claim_id, package_hash):
        print(f"error: could not release {claim_id}; review is not pending")
        return 1

    print(f"released {claim_id}")
    print(f"package_hash: {package_hash}")
    print("The operator release is valid only for this exact package.")
    return 0


def handle_command(args: argparse.Namespace) -> int:
    """Dispatch the selected CLI subcommand and return an exit code."""

    command = getattr(args, "claimgate_command", None)
    if command == "review-list":
        return _review_list(args)
    if command == "review-release":
        return _review_release(args)
    print("usage: hermes claimgate {review-list,review-release} ...")
    return 2
