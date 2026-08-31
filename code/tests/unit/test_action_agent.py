"""Tests for non-authorizing post-technique model interaction."""

from types import SimpleNamespace

from action_agent.service import ActionAgentService
from core.models import ActionCommand
from proposer.gemini import GeminiProposer


def test_action_agent_answers_without_executing() -> None:
    requests = []

    def generate_content(**kwargs):
        requests.append(kwargs)
        return response

    response = SimpleNamespace(
        text="It is a preinstalled gateway tool. No action has executed.",
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
        tool_source="execution/gateway/gcs_upload.py",
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

    assert answer == "It is a preinstalled gateway tool. No action has executed."
    config = requests[0]["config"]
    assert config.max_output_tokens == 4096
    assert config.thinking_config.include_thoughts is False
