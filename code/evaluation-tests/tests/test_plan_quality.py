from __future__ import annotations

import json
from pathlib import Path

import pytest
from evaluation_tests.plan_quality import (
    classification_metrics,
    combine_quality_gate,
    load_dataset,
    load_judgments,
    load_rubric,
    verify_plan,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def dataset():
    return load_dataset(ROOT / "cases" / "plan-quality-cases.json")


@pytest.fixture(scope="module")
def rubric():
    return load_rubric(ROOT / "rubrics" / "plan-quality-rubric.json")


def test_calibration_set_exposes_semantic_gap(dataset) -> None:
    results = {case.case_id: verify_plan(dataset, case) for case in dataset.cases}
    assert results["valid-reference-chain"].acceptable
    assert not results["unknown-technique"].acceptable
    assert not results["covered-action"].acceptable
    assert results["hard-valid-but-redundant"].acceptable
    assert results["hard-valid-but-overclaimed"].acceptable


def test_hard_check_failure_codes_match_reference_for_deterministic_cases(dataset) -> None:
    semantic_only = {"hard-valid-but-redundant", "hard-valid-but-overclaimed"}
    for case in dataset.cases:
        if case.case_id in semantic_only:
            continue
        result = verify_plan(dataset, case)
        assert result.acceptable == case.reference.acceptable
        assert set(result.failure_codes) == set(case.reference.failure_codes)


def test_hard_gate_metrics_count_two_false_accepts(dataset) -> None:
    results = [verify_plan(dataset, case) for case in dataset.cases]
    metrics = classification_metrics(
        [case.reference.acceptable for case in dataset.cases],
        [result.acceptable for result in results],
    )
    assert metrics["n"] == 12
    assert metrics["true_accept"] == 3
    assert metrics["true_reject"] == 7
    assert metrics["false_accept"] == 2
    assert metrics["false_reject"] == 0


def test_quality_judge_cannot_override_hard_failure(tmp_path, dataset, rubric) -> None:
    judgment = {
        "case_id": "covered-action",
        "rubric_id": "plan-quality-v1",
        "judge_model": "fixture-judge",
        "decision": "accept",
        "scores": {name: 5 for name in rubric["dimensions"]},
        "failure_codes": [],
        "evidence": [{"step": 1, "finding": "fixture"}],
        "rationale": "A deliberately overconfident fixture assessment.",
    }
    path = tmp_path / "judgments.jsonl"
    path.write_text(json.dumps(judgment) + "\n", encoding="utf-8")
    assessment = load_judgments(path, rubric, {case.case_id for case in dataset.cases})[
        "covered-action"
    ]
    case = next(case for case in dataset.cases if case.case_id == "covered-action")
    combined = combine_quality_gate(verify_plan(dataset, case), assessment, rubric)
    assert not combined.accepted
    assert "deterministic hard check failed" in combined.reasons


def test_judgment_requires_every_rubric_dimension(tmp_path, dataset, rubric) -> None:
    judgment = {
        "case_id": "valid-reference-chain",
        "rubric_id": "plan-quality-v1",
        "judge_model": "fixture-judge",
        "decision": "accept",
        "scores": {"objective_alignment": 5},
        "failure_codes": [],
        "evidence": [],
        "rationale": "Incomplete fixture.",
    }
    path = tmp_path / "judgments.jsonl"
    path.write_text(json.dumps(judgment) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="exactly the rubric dimensions"):
        load_judgments(path, rubric, {case.case_id for case in dataset.cases})
