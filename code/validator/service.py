from __future__ import annotations

import os

from core.config import Paths
from core.errors import DataConsistencyError, NotFoundError, VersionConflictError
from core.ids import new_id
from core.models import (
    CandidateValidationRequest,
    CandidateValidationResult,
    MatrixSnapshot,
    StateValidationRequest,
    StateValidationResult,
)
from ingestion.snapshot import SnapshotRepository
from validator.bitset import BitsetEvaluator, bits_from_indices, indices_from_bits
from validator.smt import SMTEvaluator


class ValidatorService:
    def __init__(
        self,
        snapshots: SnapshotRepository | None = None,
        paths: Paths | None = None,
        engine: str | None = None,
        verify_equivalence: bool | None = None,
    ) -> None:
        paths = paths or Paths()
        self.snapshots = snapshots or SnapshotRepository(paths.artifacts / "snapshots")
        self.engine = engine or os.getenv("VALIDATOR_ENGINE", "bitset")
        self.verify_equivalence = (
            verify_equivalence
            if verify_equivalence is not None
            else os.getenv("VALIDATOR_VERIFY_SMT", "true").lower() == "true"
        )
        if self.engine not in {"bitset", "smt"}:
            raise ValueError("VALIDATOR_ENGINE must be 'bitset' or 'smt'")

    def validate_state(self, request: StateValidationRequest) -> StateValidationResult:
        matrix = self._matrix(request.matrix_version or request.state.matrix_version)
        self._validate_state_alignment(
            request.state.matrix_version, request.state.permission_indices, matrix
        )
        width = len(matrix.permissions)
        state = bits_from_indices(request.state.permission_indices)
        coverage = bits_from_indices(matrix.coverage_indices)
        direct = BitsetEvaluator.state(state, coverage, width)
        smt = SMTEvaluator.state(state, coverage, width) if self._needs_smt() else None
        if smt is not None and smt != direct:
            raise DataConsistencyError("SMT and direct state evaluators disagree")
        result = smt if self.engine == "smt" else direct
        assert result is not None
        monitored_indices = indices_from_bits(result.monitored)
        unmonitored_indices = indices_from_bits(result.unmonitored)
        return StateValidationResult(
            result_id=new_id("state_validation"),
            state_version=request.state.state_version,
            matrix_version=matrix.matrix_version,
            monitored_permission_indices=monitored_indices,
            unmonitored_permission_indices=unmonitored_indices,
            monitored_permissions=tuple(matrix.permissions[index] for index in monitored_indices),
            unmonitored_permissions=tuple(
                matrix.permissions[index] for index in unmonitored_indices
            ),
            has_gap=bool(unmonitored_indices),
        )

    def validate_candidate(
        self, request: CandidateValidationRequest
    ) -> CandidateValidationResult:
        matrix = self._matrix(request.matrix_version or request.state.matrix_version)
        self._validate_state_alignment(
            request.state.matrix_version, request.state.permission_indices, matrix
        )
        technique = matrix.techniques.get(request.technique_id)
        if technique is None:
            raise NotFoundError(
                f"technique {request.technique_id!r} is absent from matrix {matrix.matrix_version}"
            )

        width = len(matrix.permissions)
        state = bits_from_indices(request.state.permission_indices)
        coverage = bits_from_indices(matrix.coverage_indices)
        required = bits_from_indices(technique.required_indices)
        footprint = bits_from_indices(technique.footprint_indices)
        direct = BitsetEvaluator.candidate(state, coverage, required, footprint, width)
        smt = (
            SMTEvaluator.candidate(state, coverage, required, footprint, width)
            if self._needs_smt()
            else None
        )
        if smt is not None and smt != direct:
            raise DataConsistencyError("SMT and direct candidate evaluators disagree")
        result = smt if self.engine == "smt" else direct
        assert result is not None

        missing_indices = indices_from_bits(result.missing)
        covered_indices = indices_from_bits(result.covered_footprint)
        uncovered_indices = tuple(
            sorted(set(technique.footprint_indices) - set(covered_indices))
        )
        footprint_indices = set(technique.footprint_indices)
        matching_detections = tuple(
            detection_id
            for detection_id, detection in sorted(matrix.detections.items())
            if footprint_indices.intersection(detection.permission_indices)
        )
        admissible = result.feasible and result.outside_loaded_coverage
        return CandidateValidationResult(
            result_id=new_id("candidate_validation"),
            candidate_id=request.candidate_id,
            technique_id=request.technique_id,
            admissible=admissible,
            feasible=result.feasible,
            outside_loaded_coverage=result.outside_loaded_coverage,
            missing_permissions=tuple(matrix.permissions[index] for index in missing_indices),
            covered_permissions=tuple(matrix.permissions[index] for index in covered_indices),
            uncovered_permissions=tuple(
                matrix.permissions[index] for index in uncovered_indices
            ),
            matching_detection_ids=matching_detections,
            state_version=request.state.state_version,
            matrix_version=matrix.matrix_version,
            explanation=self._explanation(
                result.feasible,
                result.outside_loaded_coverage,
                tuple(matrix.permissions[index] for index in missing_indices),
                tuple(matrix.permissions[index] for index in covered_indices),
            ),
        )

    def _matrix(self, matrix_version: str) -> MatrixSnapshot:
        return self.snapshots.get(matrix_version)

    def _needs_smt(self) -> bool:
        return self.engine == "smt" or self.verify_equivalence

    @staticmethod
    def _validate_state_alignment(
        state_matrix_version: str,
        indices: tuple[int, ...],
        matrix: MatrixSnapshot,
    ) -> None:
        if state_matrix_version != matrix.matrix_version:
            raise VersionConflictError(
                f"state uses {state_matrix_version}, validator uses {matrix.matrix_version}"
            )
        if any(index < 0 or index >= len(matrix.permissions) for index in indices):
            raise DataConsistencyError("state contains an out-of-range permission index")

    @staticmethod
    def _explanation(
        feasible: bool,
        outside: bool,
        missing: tuple[str, ...],
        covered: tuple[str, ...],
    ) -> str:
        if feasible and outside:
            return "Feasible and outside the loaded Boolean coverage model."
        reasons: list[str] = []
        if not feasible:
            reasons.append(f"missing required permissions: {', '.join(missing)}")
        if not outside:
            reasons.append(f"covered permission footprint: {', '.join(covered)}")
        return "Rejected because " + "; ".join(reasons) + "."
