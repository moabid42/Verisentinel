"""Tests for non-authorizing post-technique model interaction."""

import json
from types import SimpleNamespace

import pytest

from action_agent.service import ActionAgentService
from core.models import ActionAuthorRequest, ActionCommand
from proposer.gemini import GeminiProposer

VALID_ACTION_SOURCE = '''import json
import sys
from pathlib import Path
from urllib.request import Request, urlopen

spec = json.loads(Path("/run/verisentinel/spec.json").read_text(encoding="utf-8"))
credential = Path("/run/verisentinel/credential").read_text(encoding="utf-8").strip()
action = spec["action"]
payload = {
    "action_id": action["action_id"],
    "approval_id": spec["approval_id"],
    "engagement_id": spec["engagement_id"],
    "expected_capabilities": action["expected_capabilities"],
    "identity": spec["identity"],
    "observed_permission_footprint": action["observed_permission_footprint"],
    "operation": action["provider_operation"],
    "parameters": spec["arguments"],
    "target": spec["target"],
}
request = Request(
    "http://verisentinel-mock:8080/execute",
    data=json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    headers={
        "Authorization": f"Bearer {credential}",
        "Content-Type": "application/json",
    },
    method="POST",
)
with urlopen(request, timeout=spec["timeout_seconds"]) as response:
    sys.stdout.buffer.write(response.read(spec["output_limit_bytes"] + 1))
'''


def author_request() -> ActionAuthorRequest:
    return ActionAuthorRequest(
        engagement_id="engagement",
        approval_id="approval_" + "1" * 32,
        action_id="technique:test",
        technique_id="test",
        technique_title="Test technique",
        objective="Create the scenario object.",
        identity="service-account@example.test",
        target="projects/project-name/buckets/scenario-bucket",
        rationale="Use the validated storage action.",
        required_permissions=("storage.objects.create",),
        observed_permissions=("storage.objects.create",),
        expected_capabilities=("scenario-object-created",),
    )


def test_action_agent_answers_without_executing() -> None:
    requests = []

    def generate_content(**kwargs):
        requests.append(kwargs)
        return response

    response = SimpleNamespace(
        text="Gemini proposed this file; no action has executed.",
        candidates=(),
        usage_metadata=None,
    )
    models = SimpleNamespace(generate_content=generate_content)
    gemini = GeminiProposer(
        client=SimpleNamespace(models=models),
        maximum_attempts=1,
    )
    command = ActionCommand(
        action_id="technique:technique-id",
        approval_id="approval_" + "1" * 32,
        display="registered-command",
        tool_source="action.py",
    )
    card = SimpleNamespace(
        action_command=command,
        proposal=SimpleNamespace(
            technique_id="technique-id",
            rationale="Use the registered storage operation.",
        ),
    )

    answer = ActionAgentService(gemini).answer(
        card,
        "Where did this file come from?",
    )

    assert answer == "Gemini proposed this file; no action has executed."
    config = requests[0]["config"]
    assert config.max_output_tokens == 4096
    assert config.thinking_config.include_thoughts is False


def test_action_agent_authors_an_unwritten_validated_file() -> None:
    response = SimpleNamespace(
        text=json.dumps(
            {
                "explanation": "Send the approved envelope through the private gateway.",
                "file_path": "action.py",
                "file_content": VALID_ACTION_SOURCE,
            }
        ),
        candidates=(),
        usage_metadata=None,
    )
    requests = []

    def generate_content(**kwargs):
        requests.append(kwargs)
        return response

    gemini = GeminiProposer(
        client=SimpleNamespace(
            models=SimpleNamespace(generate_content=generate_content)
        ),
        maximum_attempts=1,
    )

    command = ActionAgentService(gemini).author(author_request())

    assert command.prepared_by == "model"
    assert command.display == "/usr/local/bin/python /workspace/action.py"
    assert command.artifact is not None
    assert command.artifact.content == VALID_ACTION_SOURCE
    assert command.artifact.written is False
    assert command.tool_installation.endswith("not written until file approval.")
    assert command.side_effects == (
        "Create gs://scenario-bucket/actions/approval_"
        + "1" * 32
        + ".json",
    )
    config = requests[0]["config"]
    assert config.max_output_tokens == 8192
    assert config.thinking_config.include_thoughts is False


def test_action_agent_rejects_source_outside_capsule_contract() -> None:
    response = SimpleNamespace(
        text=json.dumps(
            {
                "explanation": "Run a shell.",
                "file_path": "action.py",
                "file_content": "import subprocess\nsubprocess.run(['sh'])\n",
            }
        ),
        candidates=(),
        usage_metadata=None,
    )
    gemini = GeminiProposer(
        client=SimpleNamespace(
            models=SimpleNamespace(generate_content=lambda **kwargs: response)
        ),
        maximum_attempts=1,
    )

    with pytest.raises(ValueError, match="unsupported module"):
        ActionAgentService(gemini).author(author_request())
