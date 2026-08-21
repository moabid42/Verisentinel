"""Fake-client tests for the agentic tool loop (proposer/agentic.py).

No network: a scripted fake client returns a function-call response first, then a JSON
finalization, so the loop's dispatch, tool-result feedback, and finalization are all exercised.
"""

from __future__ import annotations

import pytest
from evaluation_pipeline.knowledge import build_knowledge
from evaluation_pipeline.prompt_builder import PromptBuilder
from evaluation_pipeline.scenario import EvaluationScenario
from evaluation_pipeline.state import EnvState

from core.errors import DataConsistencyError
from proposer.gemini import GeminiProposer
from proposer.service import ProposerService

SCENARIO = {
    "name": "agentic-fixture",
    "objective": "Escalate via cloudbuild.",
    "operator": "eval@local.test",
    "target_scope": "projects/unit-sandbox",
    "environment": {
        "identity": "start@unit-sandbox.iam.gserviceaccount.com",
        "starting_permissions": ["cloudbuild.builds.create", "iam.serviceAccounts.actAs"],
    },
    "techniques": [
        {
            "id": "privilege-escalation:cloudbuild:1",
            "title": "Cloud Build actAs escalation",
            "tactic": "privilege-escalation",
            "service": "cloudbuild",
            "required_permissions": ["cloudbuild.builds.create", "iam.serviceAccounts.actAs"],
        },
    ],
    "detections": [],
    "solution": [{"command": "privilege-escalation:cloudbuild:1"}],
}


class _FakeFunctionCall:
    def __init__(self, name, args):
        self.name = name
        self.args = args


class _FakeResponse:
    def __init__(self, *, function_calls=None, text=None):
        self.function_calls = function_calls or []
        self.text = text
        self.candidates = []


class _FakeModels:
    def __init__(self, script):
        self._script = list(script)
        self.calls = []

    def generate_content(self, *, model, contents, config):
        self.calls.append(config)
        return self._script.pop(0)


class _FakeClient:
    def __init__(self, script):
        self.models = _FakeModels(script)


def _service(tmp_path, script):
    scenario = EvaluationScenario.model_validate(SCENARIO)
    knowledge = build_knowledge(scenario, tmp_path / "artifacts")
    gemini = GeminiProposer(client=_FakeClient(script), api_key="x")
    service = ProposerService(snapshots=knowledge.snapshots, gemini=gemini, agentic=True)
    return service, gemini, knowledge


def _request(knowledge):
    env = EnvState.initial(knowledge.scenario)
    builder = PromptBuilder(knowledge)
    state = builder.state_vector(env, "eng-1")
    return builder.build(
        env, state, "eng-1", relevant_technique_ids=tuple(knowledge.matrix.techniques)
    )


def test_agentic_loop_explores_then_finalizes(tmp_path):
    # Round 1: model calls a tool. Round 2: no tool calls -> finalization returns JSON choices.
    final_json = (
        '{"decision_summary": "found it", '
        '"choices": [{"technique_id": "privilege-escalation:cloudbuild:1", '
        '"rationale": "actAs path"}]}'
    )
    tool_call = _FakeFunctionCall("search_techniques", {"service": "cloudbuild"})
    script = [
        _FakeResponse(function_calls=[tool_call]),  # round 1: model calls a tool
        _FakeResponse(),  # round 2: exploration ends (no tool calls)
        _FakeResponse(text=final_json),  # finalization
    ]
    service, gemini, knowledge = _service(tmp_path, script)
    batch = service.propose(_request(knowledge))
    assert [p.technique_id for p in batch.proposals] == ["privilege-escalation:cloudbuild:1"]
    # A tool round plus a finalization call happened.
    assert len(gemini.client.models.calls) == 3


def test_agentic_rejects_hallucinated_id(tmp_path):
    final_json = (
        '{"decision_summary": "x", '
        '"choices": [{"technique_id": "not-in-catalog:1", "rationale": "made up"}]}'
    )
    script = [
        _FakeResponse(),  # exploration: no tool calls
        _FakeResponse(text=final_json),  # finalization returns a hallucinated id
    ]
    service, gemini, knowledge = _service(tmp_path, script)
    with pytest.raises(DataConsistencyError):
        service.propose(_request(knowledge))
