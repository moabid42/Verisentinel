"""Regression coverage for the tracked GCS action-delivery scenario."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from runner.scenario import load_scenario

CODE_DIRECTORY = Path(__file__).resolve().parents[2]
SCENARIO_DIRECTORY = CODE_DIRECTORY / "scenarios" / "gcs_action_delivery_flag"
SCENARIO_PATH = SCENARIO_DIRECTORY / "scenario.yaml"
VERIFIER_PATH = SCENARIO_DIRECTORY / "verify_flag.py"
TERRAFORM_DIRECTORY = SCENARIO_DIRECTORY / "terraform"
APPROVAL_ID = "approval_0123456789abcdef0123456789abcdef"


def action_envelope() -> dict[str, object]:
    """Return the exact envelope emitted for the scenario's selected action."""
    return {
        "action_id": (
            "technique:unauthenticated-access:"
            "gcp-public-buckets-privilege-escalation:6"
        ),
        "approval_id": APPROVAL_ID,
        "engagement_id": "engagement_fedcba9876543210fedcba9876543210",
        "expected_capabilities": [],
        "identity": (
            "verisentinel-flag-runner@"
            "project-551b0c2b-9622-4479-b23.iam.gserviceaccount.com"
        ),
        "observed_permission_footprint": ["storage.objects.create"],
        "operation": "catalog.technique",
        "parameters": {},
        "target": (
            "projects/project-551b0c2b-9622-4479-b23/buckets/"
            "verisentinel-flag-project-551b0c2b-9622-4479-b23"
        ),
    }


def test_tracked_flag_scenario_matches_supported_storage_action() -> None:
    scenario = load_scenario(SCENARIO_PATH)

    assert scenario.name == "gcs-action-delivery-flag"
    assert scenario.target_scope == scenario.infrastructure.path
    assert scenario.infrastructure.terraform_root == "terraform"
    assert scenario.starting_service_account.permissions == (
        "storage.objects.create",
    )
    assert scenario.detections.sources == ("sigma",)


def test_flag_scenario_terraform_owns_identity_and_bucket_access() -> None:
    main = (TERRAFORM_DIRECTORY / "main.tf").read_text(encoding="utf-8")
    terraform = (TERRAFORM_DIRECTORY / "terraform.tf").read_text(
        encoding="utf-8"
    )

    assert 'resource "google_service_account" "runner"' in main
    assert 'resource "google_storage_bucket" "action_sink"' in main
    assert 'resource "google_storage_bucket_iam_binding" "scenario_writer"' in main
    assert 'role   = "roles/storage.objectCreator"' in main
    assert "serviceAccount:${google_service_account.runner.email}" in main
    assert 'version = "7.43.0"' in terraform


def test_flag_verifier_accepts_matching_gateway_envelope(tmp_path: Path) -> None:
    envelope_path = tmp_path / "action.json"
    envelope_path.write_text(json.dumps(action_envelope()), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(VERIFIER_PATH), str(envelope_path)],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == f"FLAG{{gcs-action-delivery:{APPROVAL_ID}}}"
    assert result.stderr == ""


def test_flag_verifier_rejects_another_action(tmp_path: Path) -> None:
    envelope = action_envelope()
    envelope["action_id"] = "technique:another-action"
    envelope_path = tmp_path / "action.json"
    envelope_path.write_text(json.dumps(envelope), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(VERIFIER_PATH), str(envelope_path)],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.startswith("flag verification failed:")
