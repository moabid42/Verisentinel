"""Derive a run-specific flag from a retrieved GCS action envelope."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

EXPECTED_ACTION_ID = (
    "technique:unauthenticated-access:"
    "gcp-public-buckets-privilege-escalation:6"
)
EXPECTED_IDENTITY = (
    "verisentinel-flag-runner@"
    "project-551b0c2b-9622-4479-b23.iam.gserviceaccount.com"
)
EXPECTED_TARGET = (
    "projects/project-551b0c2b-9622-4479-b23/buckets/"
    "verisentinel-flag-project-551b0c2b-9622-4479-b23"
)
EXPECTED_KEYS = {
    "action_id",
    "approval_id",
    "engagement_id",
    "expected_capabilities",
    "identity",
    "observed_permission_footprint",
    "operation",
    "parameters",
    "target",
}
APPROVAL_PATTERN = re.compile(r"^approval_[0-9a-f]{32}$")
ENGAGEMENT_PATTERN = re.compile(r"^engagement_[0-9a-f]{32}$")


class FlagVerificationError(ValueError):
    """The supplied document is not this scenario's action envelope."""


def verify_envelope(document: object) -> str:
    """Validate one downloaded envelope and return its run-specific flag."""
    if not isinstance(document, dict) or set(document) != EXPECTED_KEYS:
        raise FlagVerificationError("action envelope has an invalid shape")
    expected_values = {
        "action_id": EXPECTED_ACTION_ID,
        "expected_capabilities": [],
        "identity": EXPECTED_IDENTITY,
        "observed_permission_footprint": ["storage.objects.create"],
        "operation": "catalog.technique",
        "parameters": {},
        "target": EXPECTED_TARGET,
    }
    if any(document.get(key) != value for key, value in expected_values.items()):
        raise FlagVerificationError("action envelope does not match the scenario")
    approval_id = document.get("approval_id")
    engagement_id = document.get("engagement_id")
    if not isinstance(approval_id, str) or APPROVAL_PATTERN.fullmatch(approval_id) is None:
        raise FlagVerificationError("action envelope has an invalid approval identifier")
    if (
        not isinstance(engagement_id, str)
        or ENGAGEMENT_PATTERN.fullmatch(engagement_id) is None
    ):
        raise FlagVerificationError("action envelope has an invalid engagement identifier")
    return f"FLAG{{gcs-action-delivery:{approval_id}}}"


def main(arguments: list[str] | None = None) -> int:
    """Read a downloaded envelope and print its verified flag."""
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    if len(arguments) != 1:
        print("usage: verify_flag.py ACTION_ENVELOPE.json", file=sys.stderr)
        return 2
    try:
        document = json.loads(Path(arguments[0]).read_text(encoding="utf-8"))
        flag = verify_envelope(document)
    except (OSError, UnicodeError, json.JSONDecodeError, FlagVerificationError) as error:
        print(f"flag verification failed: {error}", file=sys.stderr)
        return 1
    print(flag)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
