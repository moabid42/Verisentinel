"""Tests for the fixed local-capsule entrypoint and image contract."""

from __future__ import annotations

import json
from pathlib import Path
from types import TracebackType
from urllib.error import URLError

import pytest

from core.models import ActionDefinition, ExecutionSpec, TechniqueActionParameters
from execution.capsule import entrypoint

ACCESS_TOKEN = "synthetic-capsule-access-token"


class StaticResponse:
    """Bounded context-managed endpoint response."""

    def __init__(self, document: object) -> None:
        self.encoded = json.dumps(document).encode("utf-8")

    def __enter__(self) -> StaticResponse:
        return self

    def __exit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del error_type, error, traceback

    def read(self, maximum: int) -> bytes:
        return self.encoded[:maximum]


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


def test_endpoint_request_keeps_credential_out_of_payload() -> None:
    observation = {"execution_id": "execution"}
    captured: dict[str, object] = {}

    def open_request(request, *, timeout: float) -> StaticResponse:
        captured["url"] = request.full_url
        captured["authorization"] = request.get_header("Authorization")
        captured["payload"] = request.data
        captured["timeout"] = timeout
        return StaticResponse(observation)

    result = entrypoint.invoke_endpoint(
        execution_spec().model_dump(mode="json"),
        ACCESS_TOKEN,
        opener=open_request,
    )

    assert result == observation
    assert captured["url"] == entrypoint.ENDPOINT_URL
    assert captured["authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert ACCESS_TOKEN.encode() not in captured["payload"]
    payload = json.loads(captured["payload"])
    assert "command" not in payload
    assert "credential_ref" not in payload


def test_entrypoint_failure_is_bounded_and_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    spec_path = tmp_path / "spec.json"
    credential_path = tmp_path / "credential"
    spec_path.write_text(execution_spec().model_dump_json(), encoding="utf-8")
    credential_path.write_text(ACCESS_TOKEN, encoding="utf-8")

    def fail(request, *, timeout: float):
        del request, timeout
        raise URLError(f"failed with {ACCESS_TOKEN}")

    monkeypatch.setattr(entrypoint, "SPEC_PATH", spec_path)
    monkeypatch.setattr(entrypoint, "CREDENTIAL_PATH", credential_path)
    monkeypatch.setattr(entrypoint, "urlopen", fail)

    result = entrypoint.main()

    assert result == 1
    captured = capsys.readouterr()
    assert captured.err == "capsule_error:endpoint_failed\n"
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
