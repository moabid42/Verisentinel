"""Validate and summarize the single-reviewer plan inspection used for RQ3.

The automatic part checks that the frozen report actually contains the reviewer-selected action
as an admissible published candidate at every scenario step.  The semantic judgments (objective
alignment, ordering, and sufficiency of the written plan) remain explicitly human judgments from
the manifest.  This module makes those two evidence types difficult to blur in the paper.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def collect_manual_plan_review(code_root: Path, manifest_path: Path) -> dict[str, Any]:
    manifest = _load_json(manifest_path)
    cases: list[dict[str, Any]] = []

    for configured in manifest["cases"]:
        report_path = code_root / configured["report"]
        scenario_path = code_root / configured["scenario"]
        report = _load_json(report_path)
        scenario = yaml.safe_load(scenario_path.read_text(encoding="utf-8"))
        issues: list[str] = []

        if report.get("scenario") != configured["scenario_id"]:
            issues.append("report scenario id differs from the review manifest")
        if scenario.get("name") != configured["scenario_id"]:
            issues.append("scenario document id differs from the review manifest")
        if report.get("objective") != scenario.get("objective"):
            issues.append("report and scenario objectives differ")

        reviewed_steps: list[dict[str, Any]] = []
        for step in report.get("steps", ()):
            selected_id = str(step.get("expected_command", ""))
            published = list(step.get("published_plans", ()))
            selected = next(
                (
                    candidate
                    for candidate in published
                    if candidate.get("technique_id") == selected_id
                ),
                None,
            )
            if selected is None:
                issues.append(
                    f"step {step.get('step_index')}: reviewer-selected action was not published"
                )
                rank = None
                admissible = False
            else:
                rank = published.index(selected) + 1
                admissible = bool(selected.get("admissible"))
                if not admissible:
                    issues.append(
                        f"step {step.get('step_index')}: reviewer-selected action "
                        "was not admissible"
                    )
            reviewed_steps.append(
                {
                    "step_index": int(step.get("step_index", len(reviewed_steps))),
                    "selected_technique": selected_id,
                    "published_rank": rank,
                    "admissible": admissible,
                    "description": str(step.get("expected_description", "")),
                    "rationale": str((selected or {}).get("rationale", "")),
                }
            )

        expected_steps = len(scenario.get("solution", ()))
        if len(reviewed_steps) != expected_steps:
            issues.append(
                f"report has {len(reviewed_steps)} reviewed steps, scenario has {expected_steps}"
            )
        if not report.get("passed"):
            issues.append("frozen pipeline report did not pass its reference-path check")

        judgments = configured["judgments"]
        semantic_accept = all(
            bool(judgments[field])
            for field in (
                "objective_path_sufficient",
                "ordering_coherent",
                "instructions_sufficient",
                "model_boundary_acknowledged",
            )
        )
        accepted = not issues and semantic_accept
        cases.append(
            {
                "scenario": configured["scenario_id"],
                "role": configured.get("role", "core"),
                "objective": report.get("objective", ""),
                "model": report.get("model", "unknown"),
                "match_policy": report.get("match_policy", "unknown"),
                "report": configured["report"],
                "report_sha256": _hash(report_path),
                "scenario_path": configured["scenario"],
                "scenario_sha256": _hash(scenario_path),
                "steps": reviewed_steps,
                "step_count": len(reviewed_steps),
                "top_ranked_steps": sum(step["published_rank"] == 1 for step in reviewed_steps),
                "within_three_steps": sum(
                    step["published_rank"] is not None and step["published_rank"] <= 3
                    for step in reviewed_steps
                ),
                "automatic_record_checks_passed": not issues,
                "manual_judgments": judgments,
                "manual_rationale": configured["rationale"],
                "accepted_by_reviewer": accepted,
                "issues": issues,
            }
        )

    total_steps = sum(case["step_count"] for case in cases)
    return {
        "study_id": manifest["study_id"],
        "review_date": manifest["review_date"],
        "reviewer_role": manifest["reviewer_role"],
        "review_design": manifest["review_design"],
        "scenario_count": len(cases),
        "accepted_scenarios": sum(case["accepted_by_reviewer"] for case in cases),
        "total_reviewed_steps": total_steps,
        "admissible_selected_steps": sum(
            step["admissible"] for case in cases for step in case["steps"]
        ),
        "top_ranked_selected_steps": sum(case["top_ranked_steps"] for case in cases),
        "selected_steps_within_top_three": sum(case["within_three_steps"] for case in cases),
        "cases": cases,
        "limitations": manifest["limitations"],
    }


def render_reviewed_plans(review: dict[str, Any]) -> dict[str, str]:
    """Return one concise Markdown plan per reviewed scenario."""

    rendered: dict[str, str] = {}
    for case in review["cases"]:
        verdict = "sufficient" if case["accepted_by_reviewer"] else "insufficient"
        lines = [
            f"# Human-reviewed plan — {case['scenario']}",
            "",
            f"- Objective: {case['objective']}",
            f"- Frozen model report: {case['model']}",
            f"- Reviewer verdict: {verdict}",
            "- Boundary: feasibility and detection statements are relative to the scripted state "
            "and loaded Boolean coverage model.",
            "",
            "## Selected route",
            "",
        ]
        for index, step in enumerate(case["steps"], start=1):
            route_heading = (
                f"{index}. `{step['selected_technique']}` (published rank {step['published_rank']})"
            )
            lines.extend(
                [
                    route_heading,
                    f"   - {step['description']}",
                    f"   - Model rationale: {step['rationale']}",
                ]
            )
        lines.extend(["", "## Review note", "", case["manual_rationale"], ""])
        rendered[case["scenario"]] = "\n".join(lines)
    return rendered
