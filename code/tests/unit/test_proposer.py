from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from core.config import Paths
from core.errors import DataConsistencyError
from core.models import ProposalRequest
from core.tracing import DebugTrace
from ingestion.service import IngestorService
from ingestion.snapshot import SnapshotRepository
from proposer.gemini import GeminiProposer
from proposer.models import GeminiChoice, GeminiChoices
from proposer.service import ProposerService


class RecordingGemini:
    model = "gemini-test"

    def __init__(self, technique_ids: tuple[str, ...]) -> None:
        self.technique_ids = technique_ids
        self.candidates = ()

    def propose(self, request, candidates, matrix) -> GeminiChoices:
        self.candidates = candidates
        return GeminiChoices(
            decision_summary="Selected known techniques for the objective.",
            choices=[
                GeminiChoice(technique_id=technique_id, rationale="model rationale")
                for technique_id in self.technique_ids
            ],
        )


def proposal_request(matrix_version: str, **updates) -> ProposalRequest:
    values = {
        "engagement_id": "engagement",
        "objective": "gain access to storage objects",
        "state_version": "state",
        "matrix_version": matrix_version,
        "identity": "identity",
        "environment_summary": {"target_scope": "projects/sandbox"},
        "maximum_proposals": 3,
    }
    values.update(updates)
    return ProposalRequest(**values)


def test_proposer_uses_gemini_choices_to_build_only_typed_actions(tmp_path: Path) -> None:
    snapshots = SnapshotRepository(tmp_path / "snapshots")
    matrix = IngestorService(paths=Paths(), repository=snapshots).build()
    technique_ids = tuple(matrix.techniques)[:3]
    gemini = RecordingGemini(technique_ids)
    result = ProposerService(snapshots=snapshots, gemini=gemini).propose(
        proposal_request(matrix.matrix_version, relevant_technique_ids=technique_ids)
    )

    assert result.provider == "gemini"
    assert result.model == "gemini-test"
    assert [proposal.technique_id for proposal in result.proposals] == list(technique_ids)
    assert all(proposal.action_id.startswith("technique:") for proposal in result.proposals)
    assert all(proposal.target == "projects/sandbox" for proposal in result.proposals)


def test_relevant_technique_filter_is_applied_before_gemini(tmp_path: Path) -> None:
    snapshots = SnapshotRepository(tmp_path / "snapshots")
    matrix = IngestorService(paths=Paths(), repository=snapshots).build()
    technique_id = next(iter(matrix.techniques))
    gemini = RecordingGemini((technique_id,))
    service = ProposerService(snapshots=snapshots, gemini=gemini)

    service.propose(
        proposal_request(
            matrix.matrix_version,
            relevant_technique_ids=(technique_id,),
        )
    )

    assert [technique.technique_id for technique in gemini.candidates] == [technique_id]


def test_unknown_gemini_choice_is_rejected(tmp_path: Path) -> None:
    snapshots = SnapshotRepository(tmp_path / "snapshots")
    matrix = IngestorService(paths=Paths(), repository=snapshots).build()
    service = ProposerService(
        snapshots=snapshots,
        gemini=RecordingGemini(("invented-technique",)),
    )

    with pytest.raises(DataConsistencyError, match="outside its catalog"):
        service.propose(proposal_request(matrix.matrix_version))


def test_duplicate_gemini_choices_are_rejected(tmp_path: Path) -> None:
    snapshots = SnapshotRepository(tmp_path / "snapshots")
    matrix = IngestorService(paths=Paths(), repository=snapshots).build()
    technique_id = next(iter(matrix.techniques))
    service = ProposerService(
        snapshots=snapshots,
        gemini=RecordingGemini((technique_id, technique_id)),
    )

    with pytest.raises(DataConsistencyError, match="duplicate"):
        service.propose(proposal_request(matrix.matrix_version))


def test_gemini_interaction_reads_current_sdk_output_envelope(tmp_path: Path) -> None:
    snapshots = SnapshotRepository(tmp_path / "snapshots")
    matrix = IngestorService(paths=Paths(), repository=snapshots).build()
    technique = next(iter(matrix.techniques.values()))
    expected = GeminiChoices(
        decision_summary="Ranked by objective relevance.",
        choices=[GeminiChoice(technique_id=technique.technique_id, rationale="relevant")],
    )
    interaction = SimpleNamespace(
        id="interaction",
        status="completed",
        outputs=[SimpleNamespace(type="text", text=expected.model_dump_json())],
        usage=SimpleNamespace(
            total_input_tokens=100,
            total_output_tokens=20,
            total_thought_tokens=5,
            total_tool_use_tokens=0,
            total_tokens=125,
        ),
    )
    interactions = SimpleNamespace(create=lambda **kwargs: interaction)
    client = SimpleNamespace(interactions=interactions)
    conversation_path = tmp_path / "conversation.jsonl"
    progress_path = tmp_path / "progress.log"
    gemini = GeminiProposer(
        client=client,
        maximum_attempts=1,
        trace=DebugTrace(
            tmp_path / "trace.jsonl",
            "test-run",
            echo=False,
            progress_path=progress_path,
        ),
        conversation_trace=DebugTrace(
            conversation_path, "test-run", secrets=("secret",), echo=False
        ),
    )

    result = gemini.propose(
        proposal_request(matrix.matrix_version),
        (technique,),
        matrix,
    )

    assert result == expected
    conversation = conversation_path.read_text(encoding="utf-8")
    assert '"role": "system"' in conversation
    assert '"role": "user"' in conversation
    assert '"role": "assistant"' in conversation
    assert '"total_tokens": 125' in conversation
    progress = progress_path.read_text(encoding="utf-8")
    assert "the model cannot execute actions" in progress
    assert f"Asking {gemini.model} for a high-level recommendation" in progress
    assert expected.decision_summary in progress


def test_gemini_has_a_hard_deadline_and_emits_heartbeats(tmp_path: Path, monkeypatch) -> None:
    blocker = Event()
    interactions = SimpleNamespace(create=lambda **kwargs: blocker.wait(5))
    client = SimpleNamespace(interactions=interactions)
    trace_path = tmp_path / "trace.jsonl"
    monkeypatch.setenv("GEMINI_HEARTBEAT_SECONDS", "0.01")
    gemini = GeminiProposer(
        client=client,
        timeout_seconds=0.04,
        maximum_attempts=1,
        trace=DebugTrace(trace_path, "test-run", echo=False),
    )

    with pytest.raises(TimeoutError, match="hard"):
        gemini._request_with_deadline("{}", 1)

    trace = trace_path.read_text(encoding="utf-8")
    assert "request_heartbeat" in trace
    assert "request_deadline_exceeded" in trace
