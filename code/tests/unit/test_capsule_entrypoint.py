"""Tests for the fixed local-capsule entrypoint and image contract."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from core.models import ActionDefinition, ExecutionSpec, TechniqueActionParameters
from execution.capsule import entrypoint

ACCESS_TOKEN = "synthetic-capsule-access-token"
ACTION_SOURCE = "print('model-authored action')\n"
ACTION_DIGEST = "sha256:" + hashlib.sha256(ACTION_SOURCE.encode("utf-8")).hexdigest()


def execution_spec() -> ExecutionSpec:
    """Build one valid immutable capsule specification."""
    return ExecutionSpec(
        engagement_id="engagement",
        candidate_id="candidate",
        action=ActionDefinition(
            action_id="technique:known",
            technique_id="known",
            observed_permission_footprint=("storage.objects.get",),
            expected_capabilities=("capability",),
        ),
        identity="runner@example.test",
        target="projects/authorized",
        arguments=TechniqueActionParameters(),
        validator_result_id="validation",
        approval_id="approval",
        state_version="state",
        matrix_version="matrix",
        credential_ref="run/default",
        artifact_digest=ACTION_DIGEST,
        artifact_path="/workspace/action.py",
    )


def test_entrypoint_accepts_only_exact_typed_spec(tmp_path: Path) -> None:
    path = tmp_path / "spec.json"
    path.write_text(execution_spec().model_dump_json(), encoding="utf-8")

    result = entrypoint.load_spec(path)

    assert result["action"]["provider_operation"] == "catalog.technique"
    assert result["arguments"] == {}

    result["command"] = "unexpected"
    path.write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(entrypoint.CapsuleEntrypointError, match="invalid_spec"):
        entrypoint.load_spec(path)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("arguments", {"command": "unexpected"}, "invalid_parameters"),
        ("action.provider_operation", "shell", "unregistered_operation"),
        ("action.parameter_model", "free-form", "invalid_parameters"),
    ],
)
def test_entrypoint_rejects_unregistered_or_untyped_input(
    tmp_path: Path,
    field: str,
    value: object,
    error: str,
) -> None:
    document = execution_spec().model_dump(mode="json")
    if field.startswith("action."):
        document["action"][field.removeprefix("action.")] = value
    else:
        document[field] = value
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(entrypoint.CapsuleEntrypointError, match=error):
        entrypoint.load_spec(path)


def test_entrypoint_runs_exact_digest_bound_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation = {"execution_id": "execution"}
    captured: dict[str, object] = {}
    path = tmp_path / "action.py"
    path.write_text(ACTION_SOURCE, encoding="utf-8")

    def run(arguments, **kwargs):
        captured["arguments"] = arguments
        captured["options"] = kwargs
        return subprocess.CompletedProcess(
            arguments,
            returncode=0,
            stdout=json.dumps(observation).encode("utf-8"),
            stderr=b"",
        )

    monkeypatch.setattr(entrypoint.subprocess, "run", run)
    result = entrypoint.invoke_artifact(
        execution_spec().model_dump(mode="json"),
        path=path,
    )

    assert result == observation
    assert captured["arguments"] == (entrypoint.sys.executable, str(path))
    options = captured["options"]
    assert options["capture_output"] is True
    assert options["check"] is False
    assert options["timeout"] == 30.0


def test_entrypoint_failure_is_bounded_and_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    spec_path = tmp_path / "spec.json"
    credential_path = tmp_path / "credential"
    credential_path.write_text(ACCESS_TOKEN, encoding="utf-8")

    artifact_path = tmp_path / "action.py"
    artifact_path.write_text(ACTION_SOURCE, encoding="utf-8")
    spec = execution_spec().model_copy(
        update={"artifact_path": str(artifact_path)}
    )
    spec_path.write_text(spec.model_dump_json(), encoding="utf-8")

    def fail(arguments, **kwargs):
        del arguments, kwargs
        raise subprocess.SubprocessError(f"failed with {ACCESS_TOKEN}")

    monkeypatch.setattr(entrypoint, "SPEC_PATH", spec_path)
    monkeypatch.setattr(entrypoint, "CREDENTIAL_PATH", credential_path)
    monkeypatch.setattr(entrypoint, "ARTIFACT_PATH", artifact_path)
    monkeypatch.setattr(entrypoint.subprocess, "run", fail)

    result = entrypoint.main()

    assert result == 1
    captured = capsys.readouterr()
    assert captured.err == "capsule_error:artifact_failed\n"
    assert ACCESS_TOKEN not in captured.err


def test_image_uses_pinned_base_and_fixed_non_root_entrypoint() -> None:
    capsule_directory = Path(__file__).parents[2] / "execution" / "capsule"
    dockerfile = (capsule_directory / "Dockerfile").read_text(encoding="utf-8")
    image = (capsule_directory / "image.lock").read_text(encoding="utf-8").strip()

    assert dockerfile.startswith("FROM python@sha256:")
    assert "USER 65532:65532" in dockerfile
    assert (
        'ENTRYPOINT ["/usr/local/bin/python", '
        '"/opt/verisentinel/entrypoint.py"]'
    ) in dockerfile
    assert " CMD " not in f" {dockerfile} "
    assert image.startswith("verisentinel-capsule@sha256:")
    assert len(image.removeprefix("verisentinel-capsule@sha256:")) == 64
