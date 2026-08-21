from __future__ import annotations

from pathlib import Path

from evaluation_tests.manual_plan_review import (
    collect_manual_plan_review,
    render_reviewed_plans,
)

EVALUATION_ROOT = Path(__file__).resolve().parents[1]
CODE_ROOT = EVALUATION_ROOT.parent


def test_frozen_manual_plan_review_covers_every_offline_scenario() -> None:
    result = collect_manual_plan_review(
        CODE_ROOT, EVALUATION_ROOT / "cases" / "manual-plan-reviews.json"
    )
    assert result["scenario_count"] == 4
    assert result["accepted_scenarios"] == 4
    assert result["total_reviewed_steps"] == 19
    assert result["admissible_selected_steps"] == 19
    assert result["top_ranked_selected_steps"] == 18
    assert result["selected_steps_within_top_three"] == 19
    assert all(case["automatic_record_checks_passed"] for case in result["cases"])


def test_review_renderer_preserves_selected_route_and_boundary() -> None:
    result = collect_manual_plan_review(
        CODE_ROOT, EVALUATION_ROOT / "cases" / "manual-plan-reviews.json"
    )
    plans = render_reviewed_plans(result)
    assert set(plans) == {case["scenario"] for case in result["cases"]}
    assert all("loaded Boolean coverage model" in plan for plan in plans.values())
    assert "published rank 2" in plans["iamouflage-sigma-cloudbuild-actas"]
