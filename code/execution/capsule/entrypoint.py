"""Fixed entrypoint for one typed operation inside the execution capsule."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SPEC_PATH = Path("/run/verisentinel/spec.json")
CREDENTIAL_PATH = Path("/run/verisentinel/credential")
ENDPOINT_URL = "http://verisentinel-mock:8080/execute"
MAXIMUM_CREDENTIAL_BYTES = 16_384
MAXIMUM_RESPONSE_BYTES = 1_048_576

_SPEC_KEYS = {
    "engagement_id",
    "candidate_id",
    "action",
    "identity",
    "target",
    "arguments",
    "validator_result_id",
    "approval_id",
    "state_version",
    "matrix_version",
    "credential_ref",
    "timeout_seconds",
    "output_limit_bytes",
}
_ACTION_KEYS = {
    "action_id",
    "provider_operation",
    "parameter_model",
    "technique_id",
    "observed_permission_footprint",
    "expected_capabilities",
}


class CapsuleEntrypointError(RuntimeError):
    """A bounded capsule entrypoint failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def main() -> int:
    """Execute exactly one validated specification against the fixed endpoint."""
    try:
        spec = load_spec()
        credential = load_credential()
        observation = invoke_endpoint(spec, credential)
        encoded = json.dumps(
            observation,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > spec["output_limit_bytes"]:
            raise CapsuleEntrypointError("output_exceeded")
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.write(b"\n")
        return 0
    except CapsuleEntrypointError as error:
        print(f"capsule_error:{error.code}", file=sys.stderr)
        return 1
    except BaseException:
        print("capsule_error:internal_failure", file=sys.stderr)
        return 1


def load_spec(path: Path | None = None) -> dict[str, Any]:
    """Load and strictly validate the single mounted execution specification."""
    path = path or SPEC_PATH
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise CapsuleEntrypointError("invalid_spec") from None
    if not isinstance(document, dict) or set(document) != _SPEC_KEYS:
        raise CapsuleEntrypointError("invalid_spec")
    if not _valid_non_empty_strings(
        document,
        _SPEC_KEYS - {"action", "arguments", "timeout_seconds", "output_limit_bytes"},
    ):
        raise CapsuleEntrypointError("invalid_spec")

    action = document["action"]
    if not isinstance(action, dict) or set(action) != _ACTION_KEYS:
        raise CapsuleEntrypointError("invalid_spec")
    if not _valid_non_empty_strings(
        action,
        {
            "action_id",
            "provider_operation",
            "parameter_model",
            "technique_id",
        },
    ):
        raise CapsuleEntrypointError("invalid_spec")
    if action["provider_operation"] != "catalog.technique":
        raise CapsuleEntrypointError("unregistered_operation")
    if action["parameter_model"] != "technique.none.v1":
        raise CapsuleEntrypointError("invalid_parameters")
    if document["arguments"] != {}:
        raise CapsuleEntrypointError("invalid_parameters")
    if not _string_list(action["observed_permission_footprint"]):
        raise CapsuleEntrypointError("invalid_spec")
    if not _string_list(action["expected_capabilities"]):
        raise CapsuleEntrypointError("invalid_spec")

    timeout = document["timeout_seconds"]
    output_limit = document["output_limit_bytes"]
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise CapsuleEntrypointError("invalid_spec")
    if not 0 < timeout <= 300:
        raise CapsuleEntrypointError("invalid_spec")
    if isinstance(output_limit, bool) or not isinstance(output_limit, int):
        raise CapsuleEntrypointError("invalid_spec")
    if not 1024 <= output_limit <= MAXIMUM_RESPONSE_BYTES:
        raise CapsuleEntrypointError("invalid_spec")
    return document


def load_credential(path: Path | None = None) -> str:
    """Read the fixed credential file with a strict size bound."""
    path = path or CREDENTIAL_PATH
    try:
        with path.open("rb") as stream:
            value = stream.read(MAXIMUM_CREDENTIAL_BYTES + 1)
    except OSError:
        raise CapsuleEntrypointError("credential_unavailable") from None
    if not value or len(value) > MAXIMUM_CREDENTIAL_BYTES:
        raise CapsuleEntrypointError("credential_unavailable")
    try:
        credential = value.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise CapsuleEntrypointError("credential_unavailable") from None
    if not credential:
        raise CapsuleEntrypointError("credential_unavailable")
    return credential


def invoke_endpoint(
    spec: Mapping[str, Any],
    credential: str,
    *,
    opener: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Send one typed operation to the fixed private mock endpoint."""
    opener = opener or urlopen
    action = spec["action"]
    payload = {
        "action_id": action["action_id"],
        "approval_id": spec["approval_id"],
        "engagement_id": spec["engagement_id"],
        "expected_capabilities": action["expected_capabilities"],
        "identity": spec["identity"],
        "observed_permission_footprint": action[
            "observed_permission_footprint"
        ],
        "operation": action["provider_operation"],
        "parameters": spec["arguments"],
        "target": spec["target"],
    }
    request = Request(
        ENDPOINT_URL,
        data=json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {credential}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    limit = min(spec["output_limit_bytes"], MAXIMUM_RESPONSE_BYTES)
    try:
        with opener(request, timeout=float(spec["timeout_seconds"])) as response:
            encoded = response.read(limit + 1)
    except (HTTPError, URLError, OSError, TimeoutError):
        raise CapsuleEntrypointError("endpoint_failed") from None
    if len(encoded) > limit:
        raise CapsuleEntrypointError("output_exceeded")
    try:
        document = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CapsuleEntrypointError("malformed_output") from None
    if not isinstance(document, dict):
        raise CapsuleEntrypointError("malformed_output")
    return document


def _valid_non_empty_strings(
    document: Mapping[str, Any],
    keys: set[str],
) -> bool:
    return all(
        isinstance(document.get(key), str) and document[key].strip()
        for key in keys
    )


def _string_list(value: object) -> bool:
    return isinstance(value, list) and all(
        isinstance(item, str) and item.strip() for item in value
    )


if __name__ == "__main__":
    raise SystemExit(main())
