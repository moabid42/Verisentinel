#!/usr/bin/env python3
"""Run the reproducible thesis evidence suites and generate thesis artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import z3

HERE = Path(__file__).resolve().parent
CODE_ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(CODE_ROOT))
sys.path.insert(0, str(CODE_ROOT / "evaluation-pipeline"))

from evaluation_tests.corpus import collect_corpus_snapshot  # noqa: E402
from evaluation_tests.equivalence import (  # noqa: E402
    benchmark_latency,
    exhaustive_equivalence,
    random_equivalence,
)
from evaluation_tests.manual_plan_review import (  # noqa: E402
    collect_manual_plan_review,
    render_reviewed_plans,
)
from evaluation_tests.normalization_audit import collect_normalization_audit  # noqa: E402
from evaluation_tests.plan_quality import (  # noqa: E402
    classification_metrics,
    combine_quality_gate,
    load_dataset,
    load_judgments,
    load_rubric,
    verify_plan,
)
from evaluation_tests.reporting import (  # noqa: E402
    copy_figures,
    write_csv,
    write_equivalence_chart,
    write_json,
    write_latency_chart,
    write_quality_chart,
)


def main() -> int:
    arguments = _arguments()
    full = arguments.full and not arguments.quick
    result_directory = arguments.output_dir or HERE / "results" / (
        "evidence-2026-08-16" if full else "quick"
    )
    paper_image_directory = arguments.paper_image_dir

    cases_path = HERE / "cases" / "plan-quality-cases.json"
    rubric_path = HERE / "rubrics" / "plan-quality-rubric.json"
    prompt_path = HERE / "prompts" / "quality-judge.md"
    dataset = load_dataset(cases_path)
    rubric = load_rubric(rubric_path)

    hard_results = [verify_plan(dataset, case) for case in dataset.cases]
    reference = [case.reference.acceptable for case in dataset.cases]
    hard_predictions = [result.acceptable for result in hard_results]
    hard_metrics = classification_metrics(reference, hard_predictions)

    corpus_scenario_path = (
        CODE_ROOT / "evaluation-pipeline" / "scenarios" / "iamouflage-sigma-cloudbuild-actas.yaml"
    )
    corpus_snapshot = collect_corpus_snapshot(corpus_scenario_path)
    normalization_audit = collect_normalization_audit(CODE_ROOT)
    manual_review_manifest = HERE / "cases" / "manual-plan-reviews.json"
    manual_plan_review = collect_manual_plan_review(CODE_ROOT, manual_review_manifest)

    exhaustive_widths = (1, 2, 3, 4) if full else (1, 2)
    boundary_widths = (8, 63, 64, 65, 256, 10_132)
    random_cases = 10_000 if full else 60
    warmups = 100 if full else 5
    repetitions = 1_000 if full else 20

    existing_summary_path = result_directory / "summary.json"
    if arguments.reuse_measurements:
        if not existing_summary_path.is_file():
            raise FileNotFoundError(
                f"cannot reuse measurements; result file does not exist: {existing_summary_path}"
            )
        existing = json.loads(existing_summary_path.read_text(encoding="utf-8"))
        exhaustive_rows = existing["validator_equivalence"]["exhaustive"]
        randomized_rows = existing["validator_equivalence"]["randomized_boundary_cases"]
        latency_rows = existing["validator_latency"]["measurements"]
    else:
        exhaustive_rows = [asdict(result) for result in exhaustive_equivalence(exhaustive_widths)]
        randomized_rows = [
            asdict(result)
            for result in random_equivalence(boundary_widths, cases=random_cases, seed=20_260_816)
        ]
        latency_rows = [
            asdict(result)
            for result in benchmark_latency(
                (8, 64, 256, 10_132), warmups=warmups, repetitions=repetitions
            )
        ]

    judgment_summary = None
    if arguments.judgments:
        assessments = load_judgments(
            arguments.judgments,
            rubric,
            {case.case_id for case in dataset.cases},
        )
        combined = []
        by_case = {result.case_id: result for result in hard_results}
        reference_by_case = {case.case_id: case.reference.acceptable for case in dataset.cases}
        for case_id, assessment in assessments.items():
            combined.append(combine_quality_gate(by_case[case_id], assessment, rubric))
        combined_metrics = classification_metrics(
            [reference_by_case[result.case_id] for result in combined],
            [result.accepted for result in combined],
        )
        judgment_summary = {
            "source": str(arguments.judgments),
            "coverage": len(combined),
            "metrics": combined_metrics,
            "cases": [asdict(result) for result in combined],
        }

    result_directory.mkdir(parents=True, exist_ok=True)
    hard_rows = []
    cases_by_id = {case.case_id: case for case in dataset.cases}
    for result in hard_results:
        case = cases_by_id[result.case_id]
        hard_rows.append(
            {
                "case_id": result.case_id,
                "reference_acceptable": case.reference.acceptable,
                "hard_acceptable": result.acceptable,
                "reference_failure_codes": ";".join(case.reference.failure_codes),
                "hard_failure_codes": ";".join(result.failure_codes),
                "executed_steps": result.executed_steps,
            }
        )
    summary = {
        "status": "thesis-evidence-bundle",
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "full" if full else "quick",
        "source_revision": _git_revision(),
        "working_tree_dirty": _working_tree_dirty(),
        "environment": {
            "python": platform.python_version(),
            "z3": z3.get_version_string(),
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
        "inputs": {
            "cases": {"path": str(cases_path.relative_to(CODE_ROOT)), "sha256": _hash(cases_path)},
            "rubric": {
                "path": str(rubric_path.relative_to(CODE_ROOT)),
                "sha256": _hash(rubric_path),
            },
            "judge_prompt": {
                "path": str(prompt_path.relative_to(CODE_ROOT)),
                "sha256": _hash(prompt_path),
            },
            "random_seed": 20_260_816,
            "corpus_scenario": {
                "path": str(corpus_scenario_path.relative_to(CODE_ROOT)),
                "sha256": _hash(corpus_scenario_path),
            },
            "manual_plan_review": {
                "path": str(manual_review_manifest.relative_to(CODE_ROOT)),
                "sha256": _hash(manual_review_manifest),
            },
        },
        "corpus_snapshot": corpus_snapshot,
        "normalization_audit": normalization_audit,
        "manual_plan_review": manual_plan_review,
        "plan_quality_calibration": {
            "dataset_id": dataset.dataset_id,
            "metrics": hard_metrics,
            "cases": hard_rows,
            "judge_results": judgment_summary,
        },
        "validator_equivalence": {
            "exhaustive": exhaustive_rows,
            "randomized_boundary_cases": randomized_rows,
            "total_cases": sum(row["cases"] for row in exhaustive_rows + randomized_rows),
            "total_disagreements": sum(
                row["disagreements"] for row in exhaustive_rows + randomized_rows
            ),
        },
        "validator_latency": {
            "warmups": warmups,
            "repetitions": repetitions,
            "measurements": latency_rows,
        },
    }
    write_json(result_directory / "summary.json", summary)
    write_json(result_directory / "corpus-snapshot.json", corpus_snapshot)
    write_json(result_directory / "normalization-audit.json", normalization_audit)
    write_json(result_directory / "manual-plan-review.json", manual_plan_review)
    review_rows = [
        {
            "scenario": case["scenario"],
            "role": case["role"],
            "model": case["model"],
            "steps": case["step_count"],
            "top_ranked_steps": case["top_ranked_steps"],
            "within_three_steps": case["within_three_steps"],
            "automatic_record_checks_passed": case["automatic_record_checks_passed"],
            "accepted_by_reviewer": case["accepted_by_reviewer"],
            "report_sha256": case["report_sha256"],
            "scenario_sha256": case["scenario_sha256"],
        }
        for case in manual_plan_review["cases"]
    ]
    write_csv(result_directory / "manual-plan-review.csv", review_rows)
    reviewed_plan_directory = result_directory / "reviewed-plans"
    reviewed_plan_directory.mkdir(parents=True, exist_ok=True)
    for scenario_id, rendered in render_reviewed_plans(manual_plan_review).items():
        (reviewed_plan_directory / f"{scenario_id}.md").write_text(rendered, encoding="utf-8")
    write_csv(result_directory / "plan-quality-cases.csv", hard_rows)
    write_csv(result_directory / "validator-equivalence-exhaustive.csv", exhaustive_rows)
    write_csv(result_directory / "validator-equivalence-random.csv", randomized_rows)
    write_csv(result_directory / "validator-latency.csv", latency_rows)
    write_quality_chart(result_directory / "qa-calibration.svg", hard_metrics)
    write_equivalence_chart(result_directory / "validator-equivalence.svg", exhaustive_rows)
    write_latency_chart(result_directory / "validator-latency.svg", latency_rows)
    copy_figures(result_directory, paper_image_directory)

    print(
        json.dumps(
            {
                "result_directory": str(result_directory),
                "equivalence_cases": summary["validator_equivalence"]["total_cases"],
                "equivalence_disagreements": summary["validator_equivalence"][
                    "total_disagreements"
                ],
                "hard_gate_accuracy": hard_metrics["accuracy"],
                "semantic_false_accepts": hard_metrics["false_accept"],
                "normalization_audit": normalization_audit["status"],
                "manually_accepted_scenarios": manual_plan_review["accepted_scenarios"],
            },
            indent=2,
        )
    )
    return 0


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--full", action="store_true", help="run the complete evidence protocol")
    mode.add_argument("--quick", action="store_true", help="run a fast development subset")
    parser.add_argument(
        "--reuse-measurements",
        action="store_true",
        help="reuse equivalence and latency rows already present in the selected result directory",
    )
    parser.add_argument("--judgments", type=Path, help="optional frozen judge JSONL")
    parser.add_argument("--output-dir", type=Path, help="override the result directory")
    parser.add_argument(
        "--paper-image-dir",
        type=Path,
        default=CODE_ROOT.parent / "paper" / "imgs" / "evaluation",
        help="where to copy generated SVG figures",
    )
    arguments = parser.parse_args()
    if not arguments.full and not arguments.quick:
        arguments.quick = True
    return arguments


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_revision() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=CODE_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _working_tree_dirty() -> bool:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=CODE_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return bool(result.stdout.strip())


if __name__ == "__main__":
    raise SystemExit(main())
