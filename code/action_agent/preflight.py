"""Isolated deterministic contract test for a model-authored action file."""

from __future__ import annotations

import ast
import json
import subprocess
import tempfile
from pathlib import Path

from core.models import ActionAuthorRequest, ExecutionObservation
from execution.capsule.provider import CapsuleConfiguration

_ALLOWED_IMPORTS = {"json", "pathlib", "sys", "urllib.error", "urllib.request"}
_REQUIRED_SOURCE_VALUES = {
    "/run/verisentinel/spec.json",
    "/run/verisentinel/credential",
    "http://verisentinel-mock:8080/execute",
}
_DENIED_CALLS = {"__import__", "compile", "eval", "exec"}
_PREFLIGHT_CREDENTIAL = "synthetic-preflight-credential"
_MAX_DIAGNOSTIC_CHARACTERS = 2048

_SITECUSTOMIZE = r"""import json
import sys
import urllib.request
from pathlib import Path

expected = json.loads(Path("/preflight/expected.json").read_text(encoding="utf-8"))

class Response:
    status = 200

    def __init__(self, body):
        self.body = body
        self.offset = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def getcode(self):
        return self.status

    def read(self, amount=-1):
        if amount is None or amount < 0:
            amount = len(self.body) - self.offset
        chunk = self.body[self.offset:self.offset + amount]
        self.offset += len(chunk)
        return chunk

def fail(message):
    sys.stderr.write("preflight_contract_error:" + message + "\n")
    raise RuntimeError(message)

def urlopen(request, timeout=None, *args, **kwargs):
    if not isinstance(request, urllib.request.Request):
        fail("the endpoint must be called with urllib.request.Request")
    if request.full_url != "http://verisentinel-mock:8080/execute":
        fail("the request used an unsupported endpoint")
    if request.get_method() != "POST":
        fail("the gateway request must use POST")
    if timeout is None or not isinstance(timeout, (int, float)) or timeout <= 0:
        fail("urlopen requires a positive timeout")
    authorization = request.get_header("Authorization")
    if authorization != "Bearer synthetic-preflight-credential":
        fail("the request did not use the mounted credential")
    content_type = request.get_header("Content-type")
    if content_type != "application/json":
        fail("the request content type must be application/json")
    try:
        body = json.loads(request.data)
    except Exception:
        fail("the request body is not JSON")
    if body != expected["envelope"]:
        fail("the request body is not the exact typed action envelope")
    return Response(json.dumps(expected["observation"], sort_keys=True).encode("utf-8"))

urllib.request.urlopen = urlopen
"""


class ActionPreflightError(ValueError):
    """A proposed action failed its isolated deterministic contract test."""


class ActionPreflight:
    """Compile and execute one action against a fake private gateway."""

    def __init__(
        self,
        *,
        docker: str = "docker",
        image: str | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.docker = docker
        self.image = image or CapsuleConfiguration.from_environment().image
        self.timeout_seconds = timeout_seconds

    def validate(self, request: ActionAuthorRequest, content: str) -> None:
        """Reject source that cannot produce the exact typed gateway request."""
        validate_source("action.py", content)
        with tempfile.TemporaryDirectory(prefix="verisentinel-preflight-") as raw_root:
            root = Path(raw_root)
            inputs = self._write_inputs(root, request, content)
            try:
                result = subprocess.run(
                    self._command(inputs),
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=self.timeout_seconds,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise ActionPreflightError(
                    "the isolated action contract test could not complete"
                ) from error
        self._validate_result(request, result)

    @staticmethod
    def _write_inputs(
        root: Path,
        request: ActionAuthorRequest,
        content: str,
    ) -> dict[str, Path]:
        paths = {
            "action": root / "action.py",
            "credential": root / "credential",
            "expected": root / "expected.json",
            "sitecustomize": root / "sitecustomize.py",
            "spec": root / "spec.json",
        }
        envelope = action_envelope(request)
        observation = {
            "execution_id": "preflight",
            "engagement_id": request.engagement_id,
            "action_id": request.action_id,
            "identity": request.identity,
            "target": request.target,
            "success": True,
            "api_response_summary": "isolated contract test passed",
        }
        spec = {
            "engagement_id": request.engagement_id,
            "candidate_id": "preflight",
            "action": {
                "action_id": request.action_id,
                "provider_operation": "catalog.technique",
                "parameter_model": "technique.none.v1",
                "technique_id": request.technique_id,
                "observed_permission_footprint": list(request.observed_permissions),
                "expected_capabilities": list(request.expected_capabilities),
            },
            "identity": request.identity,
            "target": request.target,
            "arguments": {},
            "validator_result_id": "preflight",
            "approval_id": request.approval_id,
            "state_version": "preflight",
            "matrix_version": "preflight",
            "artifact_digest": "",
            "artifact_path": "/workspace/action.py",
            "credential_ref": "preflight",
            "timeout_seconds": 5.0,
            "output_limit_bytes": 65536,
        }
        paths["action"].write_text(content, encoding="utf-8")
        paths["credential"].write_text(_PREFLIGHT_CREDENTIAL, encoding="utf-8")
        paths["expected"].write_text(
            json.dumps({"envelope": envelope, "observation": observation}),
            encoding="utf-8",
        )
        paths["sitecustomize"].write_text(_SITECUSTOMIZE, encoding="utf-8")
        paths["spec"].write_text(json.dumps(spec), encoding="utf-8")
        return paths

    def _command(self, paths: dict[str, Path]) -> tuple[str, ...]:
        mounts = tuple(
            argument
            for name, destination in (
                ("action", "/workspace/action.py"),
                ("credential", "/run/verisentinel/credential"),
                ("expected", "/preflight/expected.json"),
                ("sitecustomize", "/preflight/sitecustomize.py"),
                ("spec", "/run/verisentinel/spec.json"),
            )
            for argument in (
                "--mount",
                f"type=bind,src={paths[name]},dst={destination},readonly",
            )
        )
        return (
            self.docker,
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--user",
            "65532:65532",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--pids-limit",
            "16",
            "--memory",
            "128m",
            "--memory-swap",
            "128m",
            "--cpus",
            "0.5",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777",
            *mounts,
            "--env",
            "PYTHONPATH=/preflight",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--entrypoint",
            "/usr/local/bin/python",
            self.image,
            "/workspace/action.py",
        )

    @staticmethod
    def _validate_result(
        request: ActionAuthorRequest,
        result: subprocess.CompletedProcess[bytes],
    ) -> None:
        diagnostic = result.stderr.decode("utf-8", errors="replace").strip()
        diagnostic = diagnostic[:_MAX_DIAGNOSTIC_CHARACTERS]
        if result.returncode != 0:
            detail = diagnostic or f"action.py exited with status {result.returncode}"
            raise ActionPreflightError(f"isolated contract test failed: {detail}")
        if diagnostic:
            raise ActionPreflightError(f"isolated contract test produced diagnostics: {diagnostic}")
        if _PREFLIGHT_CREDENTIAL.encode() in result.stdout:
            raise ActionPreflightError("isolated contract test exposed credential material")
        try:
            observation = ExecutionObservation.model_validate_json(result.stdout)
        except (ValueError, UnicodeError):
            raise ActionPreflightError("isolated contract test returned malformed output") from None
        binding = (
            observation.engagement_id,
            observation.action_id,
            observation.identity,
            observation.target,
        )
        expected = (
            request.engagement_id,
            request.action_id,
            request.identity,
            request.target,
        )
        if binding != expected or not observation.success:
            raise ActionPreflightError("isolated contract test returned an invalid observation")


def validate_source(path: str, content: str) -> None:
    """Reject source outside the narrow capsule action contract."""
    if path != "action.py":
        raise ActionPreflightError("the model must propose exactly action.py")
    try:
        tree = ast.parse(content, filename=path)
        compile(tree, path, "exec")
    except (SyntaxError, ValueError):
        raise ActionPreflightError("the proposed action.py is not valid Python") from None
    imports: set[str] = set()
    calls: set[str] = set()
    literals: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add(node.module or "")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            calls.add(node.func.id)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            literals.add(node.value)
    if imports - _ALLOWED_IMPORTS:
        raise ActionPreflightError("the proposed action.py imports an unsupported module")
    if calls.intersection(_DENIED_CALLS):
        raise ActionPreflightError("the proposed action.py uses dynamic code execution")
    if not _REQUIRED_SOURCE_VALUES.issubset(literals):
        raise ActionPreflightError("the proposed action.py does not implement the capsule contract")


def action_envelope(request: ActionAuthorRequest) -> dict[str, object]:
    """Return the exact non-secret document the private gateway accepts."""
    return {
        "action_id": request.action_id,
        "approval_id": request.approval_id,
        "engagement_id": request.engagement_id,
        "expected_capabilities": list(request.expected_capabilities),
        "identity": request.identity,
        "observed_permission_footprint": list(request.observed_permissions),
        "operation": "catalog.technique",
        "parameters": {},
        "target": request.target,
    }
