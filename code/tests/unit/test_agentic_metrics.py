"""Cost accounting for the proposer: propose_with_tools and propose() must populate
last_proposal_cost so the orchestrator can aggregate per-state metrics."""

from pathlib import Path
from types import SimpleNamespace

from core.config import Paths
from ingestion.service import IngestorService
from ingestion.snapshot import SnapshotRepository
from proposer.gemini import GeminiProposer
from proposer.models import GeminiChoice, GeminiChoices

_USAGE = SimpleNamespace(
    prompt_token_count=100,
    candidates_token_count=20,
    thoughts_token_count=5,
    tool_use_prompt_token_count=0,
    total_token_count=125,
)


def _function_call(name: str, args: dict) -> SimpleNamespace:
    return SimpleNamespace(name=name, args=args)


def _response(*, function_calls=(), text=None) -> SimpleNamespace:
    candidate = SimpleNamespace(content=SimpleNamespace(parts=[]), finish_reason="STOP")
    return SimpleNamespace(
        function_calls=list(function_calls),
        candidates=[candidate],
        text=text,
        usage_metadata=_USAGE,
    )


class _ScriptedModels:
    """Returns a fixed sequence of responses for successive generate_content calls."""

    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self._responses = iter(responses)

    def generate_content(self, **_kwargs) -> SimpleNamespace:
        return next(self._responses)


def _matrix(tmp_path: Path):
    snapshots = SnapshotRepository(tmp_path / "snapshots")
    return snapshots, IngestorService(paths=Paths(), repository=snapshots).build()


def _request(matrix_version: str):
    from core.models import ProposalRequest

    return ProposalRequest(
        engagement_id="engagement",
        objective="reach storage objects",
        state_version="state",
        matrix_version=matrix_version,
        identity="identity",
        environment_summary={"target_scope": "projects/sandbox"},
        maximum_proposals=3,
    )


def test_agentic_cost_counts_rounds_calls_tokens_and_latency(tmp_path: Path) -> None:
    _, matrix = _matrix(tmp_path)
    technique_id = next(iter(matrix.techniques))
    choices_json = GeminiChoices(
        decision_summary="picked",
        choices=[GeminiChoice(technique_id=technique_id, rationale="relevant")],
    ).model_dump_json()

    responses = [
        # Round 1: the model asks for two tools in one turn.
        _response(
            function_calls=[
                _function_call("list_services", {}),
                _function_call("search_techniques", {"keywords": "storage"}),
            ]
        ),
        # Round 2: no tool calls -> exploration ends.
        _response(text="analysis"),
        # Finalization: the schema-constrained answer.
        _response(text=choices_json),
    ]
    client = SimpleNamespace(models=_ScriptedModels(responses))
    gemini = GeminiProposer(client=client, maximum_attempts=1)

    gemini.propose_with_tools(_request(matrix.matrix_version), matrix)

    cost = gemini.last_proposal_cost
    assert cost is not None
    # 2 exploration model turns + 1 finalize call.
    assert cost.exploration_rounds == 2
    assert cost.api_calls == 3
    assert cost.tool_calls == 2
    assert cost.tool_counts == {"list_services": 1, "search_techniques": 1}
    # Tokens summed across all three model calls (125 each).
    assert cost.tokens["total"] == 375
    assert cost.tokens["input"] == 300
    assert cost.latency_seconds >= 0.0


def test_non_agentic_cost_is_populated(tmp_path: Path) -> None:
    _, matrix = _matrix(tmp_path)
    technique = next(iter(matrix.techniques.values()))
    choices_json = GeminiChoices(
        decision_summary="picked",
        choices=[GeminiChoice(technique_id=technique.technique_id, rationale="relevant")],
    ).model_dump_json()

    client = SimpleNamespace(models=_ScriptedModels([_response(text=choices_json)]))
    gemini = GeminiProposer(client=client, maximum_attempts=1)

    gemini.propose(_request(matrix.matrix_version), (technique,), matrix)

    cost = gemini.last_proposal_cost
    assert cost is not None
    assert cost.api_calls == 1
    assert cost.tool_calls == 0
    assert cost.tokens["total"] == 125
    assert cost.latency_seconds >= 0.0
