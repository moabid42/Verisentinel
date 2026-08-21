from __future__ import annotations

from z3 import BitVecVal, Solver, sat, simplify

from validator.bitset import CandidateBitsResult, StateBitsResult


class SMTEvaluator:
    """Ground BitVec encoding of the same equations used by the bitset evaluator."""

    @staticmethod
    def state(state: int, coverage: int, width: int) -> StateBitsResult:
        state_value = BitVecVal(state, width)
        coverage_value = BitVecVal(coverage, width)
        monitored = simplify(state_value & coverage_value).as_long()
        unmonitored = simplify(state_value & ~coverage_value).as_long()
        return StateBitsResult(monitored=monitored, unmonitored=unmonitored)

    @staticmethod
    def candidate(
        state: int, coverage: int, required: int, footprint: int, width: int
    ) -> CandidateBitsResult:
        state_value = BitVecVal(state, width)
        coverage_value = BitVecVal(coverage, width)
        required_value = BitVecVal(required, width)
        footprint_value = BitVecVal(footprint, width)
        missing_expr = required_value & ~state_value
        covered_expr = footprint_value & coverage_value

        solver = Solver()
        solver.add(missing_expr == BitVecVal(0, width))
        feasible = solver.check() == sat
        solver.reset()
        solver.add(covered_expr == BitVecVal(0, width))
        outside = solver.check() == sat

        return CandidateBitsResult(
            missing=simplify(missing_expr).as_long(),
            covered_footprint=simplify(covered_expr).as_long(),
            feasible=feasible,
            outside_loaded_coverage=outside,
        )
