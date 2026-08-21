"""Offline unit tests for the proposer/validator evaluation pipeline.

They inject a ScriptedProposer so no Gemini key or network is required, then assert the
orchestrator, prompt builder, validator gate, and evaluator all behave as designed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from evaluation_pipeline.knowledge import build_knowledge
from evaluation_pipeline.orchestrator import Orchestrator
from evaluation_pipeline.parser import parse
from evaluation_pipeline.runner import RunConfig, run_evaluation
from evaluation_pipeline.scenario import EvaluationScenario, load_scenario
from evaluation_pipeline.scripted_proposer import ScriptedProposer
from pydantic import ValidationError

from proposer.retrieval import TechniqueRetriever
from proposer.service import ProposerService
from validator.service import ValidatorService

HERE = Path(__file__).resolve().parents[1]  # code/evaluation-pipeline

SCENARIO = {
    "name": "unit-chain",
    "objective": "Escalate then loot, preferring undetected actions.",
    "operator": "eval@local.test",
    "target_scope": "projects/unit-sandbox",
    "environment": {
        "identity": "start@unit-sandbox.iam.gserviceaccount.com",
        "starting_permissions": ["a.read", "b.actAs", "watched.write"],
    },
    "techniques": [
        {"id": "t.recon", "required_permissions": ["a.read"]},
        {"id": "t.escalate", "required_permissions": ["b.actAs"]},
        {"id": "t.loot", "required_permissions": ["c.get"]},
        {"id": "t.noisy", "required_permissions": ["watched.write"]},
    ],
    "detections": [
        {"id": "d.watch", "permissions": ["watched.write"]},
    ],
    "solution": [
        {"command": "t.recon", "output": {"discovered_resources": ["r1"]}},
        {"command": "t.escalate", "output": {"gained_permissions": ["c.get"]}},
        {"command": "t.loot", "output": {"note": "done"}},
    ],
    "evaluation": {"match_policy": "top_ranked"},
}


def _knowledge(tmp_path: Path, scenario: EvaluationScenario):
    return build_knowledge(scenario, tmp_path / "artifacts")


def _run(knowledge, oracle, *, full_catalog=False):
    retriever = (
        TechniqueRetriever(limit=max(50, len(knowledge.matrix.techniques)))
        if full_catalog
        else None
    )
    proposer = ProposerService(
        snapshots=knowledge.snapshots,
        gemini=ScriptedProposer(oracle=oracle),
        retriever=retriever,
    )
    validator = ValidatorService(snapshots=knowledge.snapshots)
    return Orchestrator(knowledge, proposer, validator).run()


def test_scenario_loads_and_cross_references():
    scenario = EvaluationScenario.model_validate(SCENARIO)
    assert len(scenario.techniques) == 4
    assert [step.command for step in scenario.solution] == ["t.recon", "t.escalate", "t.loot"]


def test_solution_command_must_exist_in_catalog():
    bad = {**SCENARIO, "solution": [{"command": "t.missing"}]}
    with pytest.raises(ValidationError):
        EvaluationScenario.model_validate(bad)


def test_oracle_ranking_passes_full_chain(tmp_path):
    scenario = EvaluationScenario.model_validate(SCENARIO)
    knowledge = _knowledge(tmp_path, scenario)
    outcome = _run(knowledge, oracle=("t.recon", "t.escalate", "t.loot"))
    assert outcome.passed
    assert outcome.steps_followed == 3


def test_validator_rejects_detected_technique(tmp_path):
    # An oracle that floats the detected technique should still not get it published,
    # because the validator marks it inside coverage.
    scenario = EvaluationScenario.model_validate(SCENARIO)
    knowledge = _knowledge(tmp_path, scenario)
    outcome = _run(knowledge, oracle=("t.noisy", "t.recon", "t.escalate", "t.loot"))
    step0 = outcome.verdicts[0]
    published = [plan.technique_id for plan in step0.published_plans]
    assert "t.noisy" not in published  # rejected on coverage
    verdicts = {plan.technique_id: plan for plan in step0.all_plans}
    assert verdicts["t.noisy"].feasible is True
    assert verdicts["t.noisy"].outside_coverage is False


def test_deviation_stops_the_run(tmp_path):
    # An oracle that deliberately proposes escalate before recon at step 0, so the run should
    # deviate on the first step and stop.
    scenario = EvaluationScenario.model_validate(SCENARIO)
    knowledge = _knowledge(tmp_path, scenario)
    outcome = _run(knowledge, oracle=("t.escalate", "t.recon", "t.loot"))
    assert not outcome.passed
    assert outcome.verdicts[0].followed is False
    assert outcome.steps_followed < 3


def test_reproposal_across_rounds(tmp_path):
    # Force multi-round: only one proposal per round (maximum_proposals=1) and an oracle that
    # floats the DETECTED technique first. Round 1 proposes the detected one -> validator
    # rejects it -> it is excluded and its reason fed back -> round 2 must propose the
    # admissible one. A pass proves the agentic re-proposal loop ran (a one-shot filter would
    # publish nothing and fail).
    scenario = EvaluationScenario.model_validate(
        {
            "name": "repropose",
            "objective": "Reach the good action.",
            "operator": "eval@local.test",
            "target_scope": "projects/unit-sandbox",
            "environment": {
                "identity": "start@unit-sandbox.iam.gserviceaccount.com",
                "starting_permissions": ["good.perm", "watched.write"],
            },
            "techniques": [
                {"id": "t.noisy", "required_permissions": ["watched.write"]},
                {"id": "t.good", "required_permissions": ["good.perm"]},
            ],
            "detections": [{"id": "d.watch", "permissions": ["watched.write"]}],
            "solution": [{"command": "t.good"}],
            "evaluation": {
                "match_policy": "top_ranked",
                "maximum_candidates": 1,
                "maximum_proposals": 1,
                "maximum_rounds": 3,
            },
        }
    )
    knowledge = _knowledge(tmp_path, scenario)
    outcome = _run(knowledge, oracle=("t.noisy", "t.good"))
    assert outcome.passed
    step0 = outcome.verdicts[0]
    published = [plan.technique_id for plan in step0.published_plans]
    proposed = [plan.technique_id for plan in step0.all_plans]
    assert published == ["t.good"]  # only the admissible one survived
    assert "t.noisy" in proposed  # the detected one was proposed and rejected in round 1


MODE_B = {
    "name": "mode-b",
    "objective": "Reach exfiltration from the manual.",
    "operator": "eval@local.test",
    "target_scope": "projects/unit-sandbox",
    "detection_provider": "sigma",
    "environment": {"identity": "start@unit-sandbox.iam.gserviceaccount.com"},
    "solution": [{"command": "discovery:gcp-iam-and-org-policies-enum:6"}],
}


def test_mode_b_requires_detection_provider():
    bad = {k: v for k, v in MODE_B.items() if k != "detection_provider"}
    with pytest.raises(ValidationError):
        EvaluationScenario.model_validate(bad)


def test_mode_a_rejects_detection_provider():
    bad = {**SCENARIO, "detection_provider": "sigma"}
    with pytest.raises(ValidationError):
        EvaluationScenario.model_validate(bad)


def test_mode_b_rejects_inline_detections():
    bad = {**MODE_B, "detections": [{"id": "d.x", "permissions": ["a.read"]}]}
    with pytest.raises(ValidationError):
        EvaluationScenario.model_validate(bad)


def test_iamouflage_scenario_builds_and_runs_offline(tmp_path):
    scenario_path = HERE / "scenarios" / "iamouflage-sigma-cloudbuild-actas.yaml"
    knowledge = parse(scenario_path, tmp_path / "artifacts")
    # Catalog + coverage come from the real manual, not the (absent) inline lists.
    assert len(knowledge.matrix.techniques) > 100
    assert knowledge.matrix.detections  # sigma corpus resolved to at least one row
    assert all(row.source == "sigma" for row in knowledge.matrix.detections.values())
    oracle = tuple(step.command for step in knowledge.scenario.solution)
    outcome = _run(knowledge, oracle=oracle, full_catalog=True)
    assert outcome.passed
    assert outcome.steps_followed == outcome.total_steps == 3


def test_metrics_are_aggregated_per_state(tmp_path):
    scenario = EvaluationScenario.model_validate(SCENARIO)
    knowledge = _knowledge(tmp_path, scenario)
    outcome = _run(knowledge, oracle=("t.recon", "t.escalate", "t.loot"))
    assert outcome.metrics is not None
    summary = outcome.metrics.summary()
    # One state per evaluated solution step, each with at least one proposal round.
    assert len(summary["per_state"]) == len(outcome.verdicts)
    assert summary["totals"]["states"] == len(outcome.verdicts)
    assert all(state["proposal_rounds"] >= 1 for state in summary["per_state"])
    # The offline scripted proposer reports no model cost, so model calls stay zero.
    assert summary["totals"]["model_calls"] == 0


def test_report_includes_metrics_block(tmp_path):
    run_evaluation(
        RunConfig(
            scenario_path=HERE / "scenario.yaml",
            offline=True,
            run_root=tmp_path,
            quiet_trace=True,
        )
    )
    run_directory = next(path for path in tmp_path.iterdir() if path.is_dir())
    report = json.loads((run_directory / "report.json").read_text(encoding="utf-8"))
    assert report["metrics"] is not None
    assert len(report["metrics"]["per_state"]) == len(report["steps"])
    assert "totals" in report["metrics"] and "averages" in report["metrics"]
    assert "## Metrics" in (run_directory / "report.md").read_text(encoding="utf-8")


def test_bundled_scenario_file_passes_offline(tmp_path):
    scenario_path = HERE / "scenario.yaml"
    scenario = load_scenario(scenario_path)
    knowledge = build_knowledge(scenario, tmp_path / "artifacts")
    outcome = _run(knowledge, oracle=tuple(s.command for s in scenario.solution))
    assert outcome.passed
    assert outcome.steps_followed == outcome.total_steps == 3
