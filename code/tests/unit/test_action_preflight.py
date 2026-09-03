"""Regression tests for isolated action contract preflight."""

from pathlib import Path

import pytest

from action_agent.preflight import ActionPreflight, ActionPreflightError
from core.models import ActionAuthorRequest
from tests.unit.test_action_agent import VALID_ACTION_SOURCE, author_request

RAW_SPEC_SOURCE = """import sys
from pathlib import Path
import urllib.request

spec_data = Path("/run/verisentinel/spec.json").read_bytes()
credential = Path("/run/verisentinel/credential").read_text(encoding="utf-8").strip()
request = urllib.request.Request(
    "http://verisentinel-mock:8080/execute",
    data=spec_data,
    headers={"Authorization": f"Bearer {credential}", "Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(request, timeout=5) as response:
        sys.stdout.buffer.write(response.read())
except Exception:
    sys.exit(1)
"""


def test_preflight_accepts_exact_typed_envelope() -> None:
    ActionPreflight().validate(author_request(), VALID_ACTION_SOURCE)


def test_preflight_rejects_posting_the_raw_execution_spec() -> None:
    with pytest.raises(ActionPreflightError, match="exact typed action envelope"):
        ActionPreflight().validate(author_request(), RAW_SPEC_SOURCE)


def test_preflight_rejects_unsupported_import_without_execution(
    tmp_path: Path,
) -> None:
    request = ActionAuthorRequest.model_validate(author_request().model_dump())

    with pytest.raises(ActionPreflightError, match="unsupported module"):
        ActionPreflight().validate(request, "import subprocess\n")
