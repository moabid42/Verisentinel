"""Tests for CLI-facing application services."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from core.models import MatrixSnapshot
from execution.credentials import (
    CredentialResolver,
    CredentialSourceKind,
    TokenMetadata,
    parse_credential_source,
)
from runner.application import (
    build_corpus,
    inspect_authentication,
    inspect_sandbox,
    read_corpus_status,
    validate_scenario,
)

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
PRINCIPAL = "runner@authorized-project.iam.gserviceaccount.com"
ACCESS_TOKEN = "synthetic-application-token"


class StaticTokenInspector:
    """Return deterministic public metadata for a supplied token."""

    def inspect(self, access_token: str) -> TokenMetadata:
        assert access_token == ACCESS_TOKEN
        return TokenMetadata(
            principal=PRINCIPAL,
            expires_at=NOW + timedelta(minutes=10),
        )


class StubIngestor:
    """Expose a fixed snapshot through the corpus service boundary."""

    def __init__(self) -> None:
        self.snapshot = SimpleNamespace(
            matrix_version="sha256:matrix",
            permissions=("permission.one", "permission.two"),
            detections={"detection": object()},
            techniques={"technique": object()},
        )

    def build(self, request: object) -> MatrixSnapshot:
        del request
        return cast(MatrixSnapshot, self.snapshot)

    def current(self) -> MatrixSnapshot:
        return cast(MatrixSnapshot, self.snapshot)


def test_scenario_validation_returns_validated_model(tmp_path: Path) -> None:
    path = tmp_path / "scenario.yaml"
    path.write_text(
        """name: command-test
objective: Evaluate one path
operator: operator@example.test
target_scope: projects/authorized-project
starting_service_account:
  identity: runner@authorized-project.iam.gserviceaccount.com
  credential_ref: run/default
  permissions:
    - storage.objects.get
""",
        encoding="utf-8",
    )

    scenario = validate_scenario(path)

    assert scenario.name == "command-test"


def test_authentication_inspection_returns_no_credential_material() -> None:
    resolver = CredentialResolver(
        environ={"VERISENTINEL_TOKEN": ACCESS_TOKEN},
        token_inspector=StaticTokenInspector(),
        now=lambda: NOW,
    )

    result = inspect_authentication(
        parse_credential_source("env:VERISENTINEL_TOKEN"),
        resolver=resolver,
    )

    assert result.source_kind == CredentialSourceKind.ENV
    assert result.principal == PRINCIPAL
    assert ACCESS_TOKEN not in repr(result)


def test_corpus_commands_return_bounded_snapshot_metadata() -> None:
    service = StubIngestor()

    built = build_corpus(service=service)
    current = read_corpus_status(service=service)

    assert built == current
    assert built.available
    assert built.matrix_version == "sha256:matrix"
    assert built.permission_count == 2
    assert built.detection_count == 1
    assert built.technique_count == 1


def test_sandbox_status_is_explicitly_unavailable() -> None:
    status = inspect_sandbox()

    assert not status.available
    assert status.provider == "capsule"
    assert "not installed" in status.detail
