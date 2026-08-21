from evaluation_tests.equivalence import (
    benchmark_latency,
    exhaustive_equivalence,
    random_equivalence,
)


def test_exhaustive_width_one_has_no_disagreement() -> None:
    result = exhaustive_equivalence((1,))[0]
    assert result.cases == 16
    assert result.disagreements == 0


def test_seeded_random_comparison_is_reproducible() -> None:
    first = random_equivalence((8, 64), cases=20, seed=7)
    second = random_equivalence((8, 64), cases=20, seed=7)
    assert [(item.width, item.cases, item.disagreements) for item in first] == [
        (item.width, item.cases, item.disagreements) for item in second
    ]


def test_latency_benchmark_records_both_engines() -> None:
    results = benchmark_latency((8,), warmups=1, repetitions=3)
    assert {result.engine for result in results} == {"Direct bitset", "Z3 bit-vector"}
    assert all(result.median_microseconds > 0 for result in results)
