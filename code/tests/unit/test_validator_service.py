from pathlib import Path

from core.config import Paths
from core.models import CandidateValidationRequest, StateValidationRequest, StateVector
from ingestion.service import IngestorService
from ingestion.snapshot import SnapshotRepository
from validator.bitset import BitsetEvaluator, bits_from_indices
from validator.service import ValidatorService
from validator.smt import SMTEvaluator


def test_state_equations_partition_current_access(tmp_path: Path) -> None:
    snapshots = SnapshotRepository(tmp_path / "snapshots")
    matrix = IngestorService(paths=Paths(), repository=snapshots).build()
    monitored_index = matrix.coverage_indices[0]
    unmonitored_index = next(
        index for index in range(len(matrix.permissions)) if index not in matrix.coverage_indices
    )
    state = StateVector(
        state_version="state",
        engagement_id="engagement",
        matrix_version=matrix.matrix_version,
        identity="identity",
        credential="credential",
        scope="projects/sandbox",
        permission_indices=(monitored_index, unmonitored_index),
        source="fixture",
    )
    result = ValidatorService(snapshots=snapshots).validate_state(
        StateValidationRequest(state=state)
    )

    assert result.monitored_permission_indices == (monitored_index,)
    assert result.unmonitored_permission_indices == (unmonitored_index,)
    assert set(result.monitored_permission_indices).isdisjoint(
        result.unmonitored_permission_indices
    )
    assert set(result.monitored_permission_indices) | set(
        result.unmonitored_permission_indices
    ) == set(state.permission_indices)


def test_candidate_requires_feasibility_and_zero_coverage_intersection(tmp_path: Path) -> None:
    snapshots = SnapshotRepository(tmp_path / "snapshots")
    matrix = IngestorService(paths=Paths(), repository=snapshots).build()
    uncovered = next(
        technique
        for technique in matrix.techniques.values()
        if not set(technique.footprint_indices).intersection(matrix.coverage_indices)
    )
    state = StateVector(
        state_version="state",
        engagement_id="engagement",
        matrix_version=matrix.matrix_version,
        identity="identity",
        credential="credential",
        scope="projects/sandbox",
        permission_indices=uncovered.required_indices,
        source="fixture",
    )
    result = ValidatorService(snapshots=snapshots).validate_candidate(
        CandidateValidationRequest(
            candidate_id="candidate", technique_id=uncovered.technique_id, state=state
        )
    )

    assert result.feasible
    assert result.outside_loaded_coverage
    assert result.admissible
    assert result.uncovered_permissions == tuple(
        matrix.permissions[index] for index in uncovered.footprint_indices
    )


def test_direct_and_smt_evaluators_agree() -> None:
    width = 8
    state = bits_from_indices((0, 1, 4, 7))
    coverage = bits_from_indices((1, 3, 4))
    required = bits_from_indices((0, 7))
    footprint = bits_from_indices((0, 2))

    assert BitsetEvaluator.state(state, coverage, width) == SMTEvaluator.state(
        state, coverage, width
    )
    assert BitsetEvaluator.candidate(
        state, coverage, required, footprint, width
    ) == SMTEvaluator.candidate(state, coverage, required, footprint, width)
