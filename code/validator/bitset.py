from __future__ import annotations

from dataclasses import dataclass


def bits_from_indices(indices: tuple[int, ...] | list[int]) -> int:
    bits = 0
    for index in indices:
        bits |= 1 << index
    return bits


def indices_from_bits(bits: int) -> tuple[int, ...]:
    indices: list[int] = []
    while bits:
        least_significant = bits & -bits
        indices.append(least_significant.bit_length() - 1)
        bits ^= least_significant
    return tuple(indices)


@dataclass(frozen=True, slots=True)
class StateBitsResult:
    monitored: int
    unmonitored: int


@dataclass(frozen=True, slots=True)
class CandidateBitsResult:
    missing: int
    covered_footprint: int
    feasible: bool
    outside_loaded_coverage: bool


class BitsetEvaluator:
    @staticmethod
    def state(state: int, coverage: int, width: int) -> StateBitsResult:
        universe = (1 << width) - 1
        monitored = state & coverage
        unmonitored = state & (~coverage & universe)
        return StateBitsResult(monitored=monitored, unmonitored=unmonitored)

    @staticmethod
    def candidate(
        state: int, coverage: int, required: int, footprint: int, width: int
    ) -> CandidateBitsResult:
        universe = (1 << width) - 1
        missing = required & (~state & universe)
        covered_footprint = footprint & coverage
        return CandidateBitsResult(
            missing=missing,
            covered_footprint=covered_footprint,
            feasible=missing == 0,
            outside_loaded_coverage=covered_footprint == 0,
        )

