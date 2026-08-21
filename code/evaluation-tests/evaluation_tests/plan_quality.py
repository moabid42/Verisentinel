"""Hard plan checks and the independent quality-judge output contract.

The deterministic verifier decides only facts represented in the calibration scenario. The
quality judge may reject a hard-valid plan for a semantic defect, but can never turn a hard
failure into an acceptance.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class Technique:
    technique_id: str
    requires: frozenset[str]
    footprint: frozenset[str]
    grants_permissions: frozenset[str]
    grants_facts: frozenset[str]


@dataclass(frozen=True, slots=True)
class PlanStep:
    technique_id: str
    rationale: str


@dataclass(frozen=True, slots=True)
class ReferenceLabel:
    acceptable: bool
    failure_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PlanCase:
    case_id: str
    plan: tuple[PlanStep, ...]
    reference: ReferenceLabel


@dataclass(frozen=True, slots=True)
class PlanQualityDataset:
    dataset_id: str
    objective: str
    initial_permissions: frozenset[str]
    initial_facts: frozenset[str]
    covered_permissions: frozenset[str]
    goal_facts: frozenset[str]
    techniques: dict[str, Technique]
    cases: tuple[PlanCase, ...]


@dataclass(frozen=True, slots=True)
class HardCheckFinding:
    step: int | None
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class HardCheckResult:
    case_id: str
    acceptable: bool
    failure_codes: tuple[str, ...]
    findings: tuple[HardCheckFinding, ...]
    executed_steps: int
    final_permissions: tuple[str, ...]
    final_facts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class JudgeEvidence:
    step: int | None
    finding: str


@dataclass(frozen=True, slots=True)
class JudgeAssessment:
    case_id: str
    rubric_id: str
    judge_model: str
    decision: str
    scores: dict[str, int]
    failure_codes: tuple[str, ...]
    evidence: tuple[JudgeEvidence, ...]
    rationale: str


@dataclass(frozen=True, slots=True)
class QualityGateResult:
    case_id: str
    accepted: bool
    hard_accepted: bool
    judge_accepted: bool
    weighted_mean: float
    minimum_score: int
    reasons: tuple[str, ...]


def load_dataset(path: Path) -> PlanQualityDataset:
    document = json.loads(path.read_text(encoding="utf-8"))
    scenario = _mapping(document.get("scenario"), "scenario")
    raw_techniques = _list(scenario.get("techniques"), "scenario.techniques")
    techniques: dict[str, Technique] = {}
    for index, item in enumerate(raw_techniques):
        raw = _mapping(item, f"scenario.techniques[{index}]")
        technique_id = _nonempty_string(raw.get("id"), f"scenario.techniques[{index}].id")
        if technique_id in techniques:
            raise ValueError(f"duplicate technique id: {technique_id}")
        techniques[technique_id] = Technique(
            technique_id=technique_id,
            requires=frozenset(_strings(raw.get("requires", []), "requires")),
            footprint=frozenset(_strings(raw.get("footprint", []), "footprint")),
            grants_permissions=frozenset(
                _strings(raw.get("grants_permissions", []), "grants_permissions")
            ),
            grants_facts=frozenset(_strings(raw.get("grants_facts", []), "grants_facts")),
        )

    cases: list[PlanCase] = []
    case_ids: set[str] = set()
    for index, item in enumerate(_list(document.get("cases"), "cases")):
        raw = _mapping(item, f"cases[{index}]")
        case_id = _nonempty_string(raw.get("case_id"), f"cases[{index}].case_id")
        if case_id in case_ids:
            raise ValueError(f"duplicate case id: {case_id}")
        case_ids.add(case_id)
        plan = tuple(
            PlanStep(
                technique_id=_nonempty_string(
                    _mapping(step, "plan step").get("technique_id"), "technique_id"
                ),
                rationale=str(_mapping(step, "plan step").get("rationale", "")).strip(),
            )
            for step in _list(raw.get("plan"), f"cases[{index}].plan")
        )
        reference_raw = _mapping(raw.get("reference"), f"cases[{index}].reference")
        acceptable = reference_raw.get("acceptable")
        if not isinstance(acceptable, bool):
            raise ValueError(f"cases[{index}].reference.acceptable must be Boolean")
        cases.append(
            PlanCase(
                case_id=case_id,
                plan=plan,
                reference=ReferenceLabel(
                    acceptable=acceptable,
                    failure_codes=tuple(
                        _strings(reference_raw.get("failure_codes", []), "failure_codes")
                    ),
                ),
            )
        )

    return PlanQualityDataset(
        dataset_id=_nonempty_string(document.get("dataset_id"), "dataset_id"),
        objective=_nonempty_string(scenario.get("objective"), "scenario.objective"),
        initial_permissions=frozenset(
            _strings(scenario.get("initial_permissions", []), "initial_permissions")
        ),
        initial_facts=frozenset(_strings(scenario.get("initial_facts", []), "initial_facts")),
        covered_permissions=frozenset(
            _strings(scenario.get("covered_permissions", []), "covered_permissions")
        ),
        goal_facts=frozenset(_strings(scenario.get("goal_facts", []), "goal_facts")),
        techniques=techniques,
        cases=tuple(cases),
    )


def verify_plan(dataset: PlanQualityDataset, case: PlanCase) -> HardCheckResult:
    permissions = set(dataset.initial_permissions)
    facts = set(dataset.initial_facts)
    findings: list[HardCheckFinding] = []
    executed_steps = 0
    terminal_detection = False

    if not case.plan:
        findings.append(HardCheckFinding(None, "empty_plan", "the plan has no steps"))

    for position, step in enumerate(case.plan, start=1):
        if terminal_detection:
            break
        technique = dataset.techniques.get(step.technique_id)
        if technique is None:
            findings.append(
                HardCheckFinding(position, "unknown_technique", step.technique_id)
            )
            continue

        missing = sorted(technique.requires - permissions)
        if missing:
            findings.append(
                HardCheckFinding(
                    position,
                    "unmet_precondition",
                    ", ".join(missing),
                )
            )
            continue

        executed_steps += 1
        covered = sorted(technique.footprint & dataset.covered_permissions)
        if covered:
            findings.append(
                HardCheckFinding(position, "covered_footprint", ", ".join(covered))
            )
            terminal_detection = True
            continue

        permissions.update(technique.grants_permissions)
        facts.update(technique.grants_facts)

    missing_goal = sorted(dataset.goal_facts - facts)
    if missing_goal:
        findings.append(
            HardCheckFinding(None, "objective_not_reached", ", ".join(missing_goal))
        )

    failure_codes = tuple(dict.fromkeys(finding.code for finding in findings))
    return HardCheckResult(
        case_id=case.case_id,
        acceptable=not failure_codes,
        failure_codes=failure_codes,
        findings=tuple(findings),
        executed_steps=executed_steps,
        final_permissions=tuple(sorted(permissions)),
        final_facts=tuple(sorted(facts)),
    )


def load_rubric(path: Path) -> dict[str, Any]:
    rubric = json.loads(path.read_text(encoding="utf-8"))
    _nonempty_string(rubric.get("rubric_id"), "rubric_id")
    dimensions = _mapping(rubric.get("dimensions"), "dimensions")
    if not dimensions:
        raise ValueError("rubric must define at least one dimension")
    return rubric


def load_judgments(
    path: Path, rubric: dict[str, Any], known_case_ids: set[str]
) -> dict[str, JudgeAssessment]:
    assessments: dict[str, JudgeAssessment] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = _mapping(json.loads(line), f"line {line_number}")
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid JSON on judgment line {line_number}: {error.msg}") from error
        assessment = _parse_assessment(raw, rubric, line_number)
        if assessment.case_id not in known_case_ids:
            raise ValueError(f"judgment references unknown case: {assessment.case_id}")
        if assessment.case_id in assessments:
            raise ValueError(f"duplicate judgment for case: {assessment.case_id}")
        assessments[assessment.case_id] = assessment
    return assessments


def combine_quality_gate(
    hard: HardCheckResult,
    assessment: JudgeAssessment,
    rubric: dict[str, Any],
) -> QualityGateResult:
    if hard.case_id != assessment.case_id:
        raise ValueError("hard-check and judge case ids differ")
    dimensions = _mapping(rubric["dimensions"], "dimensions")
    weighted_total = sum(
        assessment.scores[name] * float(_mapping(spec, name).get("weight", 1.0))
        for name, spec in dimensions.items()
    )
    total_weight = sum(
        float(_mapping(spec, name).get("weight", 1.0)) for name, spec in dimensions.items()
    )
    weighted_mean = weighted_total / total_weight
    minimum_score = min(assessment.scores.values())
    gate = _mapping(rubric["gate"], "gate")
    minimum_mean = float(gate["minimum_weighted_mean"])
    minimum_dimension = int(gate["minimum_dimension_score"])
    judge_accepted = assessment.decision == "accept"

    reasons: list[str] = []
    if not hard.acceptable:
        reasons.append("deterministic hard check failed")
    if not judge_accepted:
        reasons.append("quality judge rejected the plan")
    if weighted_mean < minimum_mean:
        reasons.append("weighted rubric mean is below threshold")
    if minimum_score < minimum_dimension:
        reasons.append("at least one rubric dimension is below threshold")

    return QualityGateResult(
        case_id=hard.case_id,
        accepted=not reasons,
        hard_accepted=hard.acceptable,
        judge_accepted=judge_accepted,
        weighted_mean=weighted_mean,
        minimum_score=minimum_score,
        reasons=tuple(reasons),
    )


def classification_metrics(
    reference: list[bool], predictions: list[bool]
) -> dict[str, float | int]:
    if len(reference) != len(predictions) or not reference:
        raise ValueError("reference and predictions must be non-empty and equally sized")
    pairs = list(zip(reference, predictions, strict=True))
    true_accept = sum(expected and predicted for expected, predicted in pairs)
    true_reject = sum(
        (not expected) and (not predicted) for expected, predicted in pairs
    )
    false_accept = sum((not expected) and predicted for expected, predicted in pairs)
    false_reject = sum(expected and (not predicted) for expected, predicted in pairs)
    total = len(reference)
    accept_precision = _divide(true_accept, true_accept + false_accept)
    accept_recall = _divide(true_accept, true_accept + false_reject)
    reject_recall = _divide(true_reject, true_reject + false_accept)
    observed = (true_accept + true_reject) / total
    reference_accept_rate = (true_accept + false_reject) / total
    predicted_accept_rate = (true_accept + false_accept) / total
    chance = reference_accept_rate * predicted_accept_rate + (
        1 - reference_accept_rate
    ) * (1 - predicted_accept_rate)
    kappa = _divide(observed - chance, 1 - chance)
    return {
        "n": total,
        "true_accept": true_accept,
        "true_reject": true_reject,
        "false_accept": false_accept,
        "false_reject": false_reject,
        "accuracy": observed,
        "balanced_accuracy": (accept_recall + reject_recall) / 2,
        "accept_precision": accept_precision,
        "accept_recall": accept_recall,
        "reject_recall": reject_recall,
        "cohens_kappa": kappa,
    }


def _parse_assessment(
    raw: dict[str, Any], rubric: dict[str, Any], line_number: int
) -> JudgeAssessment:
    rubric_id = _nonempty_string(raw.get("rubric_id"), "rubric_id")
    if rubric_id != rubric["rubric_id"]:
        raise ValueError(f"judgment line {line_number} uses unexpected rubric {rubric_id}")
    decision = _nonempty_string(raw.get("decision"), "decision")
    if decision not in {"accept", "reject"}:
        raise ValueError("judgment decision must be 'accept' or 'reject'")
    score_raw = _mapping(raw.get("scores"), "scores")
    expected_dimensions = set(_mapping(rubric["dimensions"], "dimensions"))
    if set(score_raw) != expected_dimensions:
        raise ValueError("judgment scores must contain exactly the rubric dimensions")
    scale = _mapping(rubric["scale"], "scale")
    minimum = int(scale["minimum"])
    maximum = int(scale["maximum"])
    scores: dict[str, int] = {}
    for dimension, score in score_raw.items():
        if isinstance(score, bool) or not isinstance(score, int) or not minimum <= score <= maximum:
            raise ValueError(f"score for {dimension} must be an integer in [{minimum}, {maximum}]")
        scores[dimension] = score
    evidence: list[JudgeEvidence] = []
    for item in _list(raw.get("evidence", []), "evidence"):
        evidence_raw = _mapping(item, "evidence item")
        step = evidence_raw.get("step")
        if step is not None and (isinstance(step, bool) or not isinstance(step, int) or step < 1):
            raise ValueError("evidence step must be a positive integer or null")
        evidence.append(
            JudgeEvidence(
                step=step,
                finding=_nonempty_string(evidence_raw.get("finding"), "evidence finding"),
            )
        )
    return JudgeAssessment(
        case_id=_nonempty_string(raw.get("case_id"), "case_id"),
        rubric_id=rubric_id,
        judge_model=_nonempty_string(raw.get("judge_model"), "judge_model"),
        decision=decision,
        scores=scores,
        failure_codes=tuple(_strings(raw.get("failure_codes", []), "failure_codes")),
        evidence=tuple(evidence),
        rationale=_nonempty_string(raw.get("rationale"), "rationale"),
    )


def _divide(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array")
    return value


def _strings(value: Any, name: str) -> list[str]:
    items = _list(value, name)
    if not all(isinstance(item, str) and item.strip() for item in items):
        raise ValueError(f"{name} must contain only non-empty strings")
    return [item.strip() for item in items]


def _nonempty_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()
