"""Boolean/SMT equivalence and latency measurements for RQ1."""

from __future__ import annotations

import random
from dataclasses import asdict, dataclass
from itertools import product
from statistics import median
from time import perf_counter, perf_counter_ns

from validator.bitset import BitsetEvaluator
from validator.smt import SMTEvaluator


@dataclass(frozen=True, slots=True)
class EquivalenceResult:
    width: int
    cases: int
    disagreements: int
    elapsed_seconds: float


@dataclass(frozen=True, slots=True)
class LatencyResult:
    engine: str
    width: int
    repetitions: int
    median_microseconds: float
    p95_microseconds: float
    minimum_microseconds: float
    maximum_microseconds: float


def exhaustive_equivalence(widths: tuple[int, ...]) -> list[EquivalenceResult]:
    results: list[EquivalenceResult] = []
    for width in widths:
        _validate_width(width)
        values = range(1 << width)
        disagreements = 0
        cases = 0
        started = perf_counter()
        for state, coverage, required, footprint in product(values, repeat=4):
            direct = BitsetEvaluator.candidate(state, coverage, required, footprint, width)
            symbolic = SMTEvaluator.candidate(state, coverage, required, footprint, width)
            disagreements += direct != symbolic
            cases += 1
        results.append(
            EquivalenceResult(
                width=width,
                cases=cases,
                disagreements=disagreements,
                elapsed_seconds=perf_counter() - started,
            )
        )
    return results


def random_equivalence(
    widths: tuple[int, ...], *, cases: int, seed: int
) -> list[EquivalenceResult]:
    if cases < 1:
        raise ValueError("cases must be positive")
    for width in widths:
        _validate_width(width)
    generator = random.Random(seed)
    counts = {width: 0 for width in widths}
    disagreements = {width: 0 for width in widths}
    elapsed = {width: 0.0 for width in widths}
    for _ in range(cases):
        width = generator.choice(widths)
        state = generator.getrandbits(width)
        coverage = generator.getrandbits(width)
        required = generator.getrandbits(width)
        footprint = generator.getrandbits(width)
        started = perf_counter()
        direct = BitsetEvaluator.candidate(state, coverage, required, footprint, width)
        symbolic = SMTEvaluator.candidate(state, coverage, required, footprint, width)
        elapsed[width] += perf_counter() - started
        counts[width] += 1
        disagreements[width] += direct != symbolic
    return [
        EquivalenceResult(
            width=width,
            cases=counts[width],
            disagreements=disagreements[width],
            elapsed_seconds=elapsed[width],
        )
        for width in widths
    ]


def benchmark_latency(
    widths: tuple[int, ...], *, warmups: int, repetitions: int
) -> list[LatencyResult]:
    if warmups < 0 or repetitions < 1:
        raise ValueError("warmups must be non-negative and repetitions must be positive")
    results: list[LatencyResult] = []
    evaluators = (("Direct bitset", BitsetEvaluator), ("Z3 bit-vector", SMTEvaluator))
    for width in widths:
        _validate_width(width)
        values = _representative_values(width)
        for name, evaluator in evaluators:
            for _ in range(warmups):
                evaluator.candidate(*values, width)
            observations: list[float] = []
            for _ in range(repetitions):
                started = perf_counter_ns()
                evaluator.candidate(*values, width)
                observations.append((perf_counter_ns() - started) / 1_000)
            observations.sort()
            results.append(
                LatencyResult(
                    engine=name,
                    width=width,
                    repetitions=repetitions,
                    median_microseconds=median(observations),
                    p95_microseconds=_percentile(observations, 0.95),
                    minimum_microseconds=observations[0],
                    maximum_microseconds=observations[-1],
                )
            )
    return results


def results_as_dict(results: list[EquivalenceResult | LatencyResult]) -> list[dict]:
    return [asdict(result) for result in results]


def _representative_values(width: int) -> tuple[int, int, int, int]:
    universe = (1 << width) - 1
    alternating = int("10" * ((width + 1) // 2), 2) & universe
    inverse = universe ^ alternating
    required = alternating & (universe >> max(1, width // 4))
    footprint = inverse | (1 << (width - 1))
    return alternating, inverse, required, footprint


def _percentile(values: list[float], probability: float) -> float:
    index = max(0, min(len(values) - 1, int(probability * len(values) + 0.999999) - 1))
    return values[index]


def _validate_width(width: int) -> None:
    if width < 1:
        raise ValueError("bit-vector width must be positive")
