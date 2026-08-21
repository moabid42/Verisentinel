"""Render ``plan.md``: the validated attack path as an executable runbook.

For every solution step the run actually *followed* — the proposer chose the technique and the
validator confirmed it is feasible from the held permissions and its footprint evades every loaded
detection — this emits the technique, the permissions it consumes and grants, the validator verdict,
and the exploitation steps pulled from the IAMouflage technique source. Steps that were never
validated are listed afterwards so the plan is honest about how far the chain actually got.

It reads only the run outcome plus the (immutable) knowledge plane, so it never re-runs anything.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from evaluation_pipeline.evaluator import ProposedPlan, StepVerdict
    from evaluation_pipeline.knowledge import Knowledge
    from evaluation_pipeline.orchestrator import EvaluationOutcome
    from evaluation_pipeline.scenario import EvaluationScenario, SolutionStep
    from ingestion.technique_source import TechniqueSourceResolver


def build_plan(
    scenario: EvaluationScenario,
    knowledge: Knowledge,
    outcome: EvaluationOutcome,
    resolver: TechniqueSourceResolver | None = None,
) -> str:
    permissions = knowledge.matrix.permissions
    provider = scenario.detection_provider or "inline"
    followed = [verdict for verdict in outcome.verdicts if verdict.followed]

    lines: list[str] = [
        f"# Attack plan — {scenario.name}",
        "",
        f"- **Objective:** {scenario.objective}",
        f"- **Identity:** {scenario.environment.identity}",
        f"- **Target scope:** {scenario.target_scope}",
        f"- **Detection model:** `{provider}` corpus",
        f"- **Result:** {'VALIDATED' if outcome.passed else 'PARTIAL'} "
        f"({outcome.steps_followed}/{outcome.total_steps} steps validated)",
        "",
    ]

    if not followed:
        lines += [
            "No step was validated: the proposer never produced an admissible technique that",
            "matched the expected path, so there is no plan to execute.",
            "",
        ]
        return "\n".join(lines)

    lines += ["## Validated path", ""]
    for position, verdict in enumerate(followed, start=1):
        description = verdict.expected_description or _title(knowledge, verdict.expected_command)
        lines.append(f"{position}. `{verdict.expected_command}` — {description}")
    lines.append("")

    lines += ["## Execution steps", ""]
    for position, verdict in enumerate(followed, start=1):
        lines += _step_section(
            position, verdict, scenario, knowledge, permissions, provider, resolver
        )

    remainder = [verdict for verdict in outcome.verdicts if not verdict.followed]
    if remainder:
        lines += ["## Unvalidated remainder", ""]
        for verdict in remainder:
            lines.append(f"- `{verdict.expected_command}` — {verdict.reason}")
        lines.append("")

    return "\n".join(lines)


def _step_section(
    position: int,
    verdict: StepVerdict,
    scenario: EvaluationScenario,
    knowledge: Knowledge,
    permissions: tuple[str, ...],
    provider: str,
    resolver: TechniqueSourceResolver | None,
) -> list[str]:
    technique_id = verdict.expected_command
    technique = knowledge.matrix.techniques.get(technique_id)
    step = _solution_step(scenario, verdict.step_index)
    plan = _published(verdict, technique_id)

    lines = [f"### Step {position}: `{technique_id}`", ""]
    if verdict.expected_description:
        lines += [verdict.expected_description, ""]

    if technique is not None:
        required = ", ".join(f"`{permissions[i]}`" for i in technique.required_indices) or "none"
        footprint = ", ".join(f"`{permissions[i]}`" for i in technique.footprint_indices) or "none"
        lines += [
            f"- **Tactic / service:** {technique.tactic or '—'} / {technique.service or '—'}",
            f"- **Requires:** {required}",
            f"- **Footprint:** {footprint}",
        ]

    lines.append(_verdict_line(plan, provider))

    if step is not None:
        gained = _gains(step)
        if gained:
            lines.append(f"- **Yields:** {gained}")
        if step.output.note:
            lines.append(f"- **Note:** {step.output.note}")
    lines.append("")

    source = resolver.resolve(technique_id) if resolver is not None else None
    if source is not None and source.markdown:
        language = "go" if source.source == "stratus" else ""
        if language:
            lines += [f"```{language}", source.markdown, "```", ""]
        else:
            lines += [f"_Runbook from IAMouflage (`{source.source}`):_", "", source.markdown, ""]
    else:
        lines += ["_No source runbook available for this technique._", ""]

    return lines


def _verdict_line(plan: ProposedPlan | None, provider: str) -> str:
    if plan is None:
        return "- **Validator:** followed (no candidate evidence recorded)."
    if plan.admissible and not plan.matching_detection_ids:
        return (
            f"- **Validator:** admissible — feasible from the held permissions and outside every "
            f"loaded `{provider}` detection."
        )
    if plan.matching_detection_ids:
        hit = ", ".join(f"`{detection}`" for detection in plan.matching_detection_ids)
        return f"- **Validator:** footprint intersects loaded detections: {hit}."
    return f"- **Validator:** {plan.explanation}"


def _published(verdict: StepVerdict, technique_id: str) -> ProposedPlan | None:
    for plan in verdict.published_plans:
        if plan.technique_id == technique_id:
            return plan
    return verdict.published_plans[0] if verdict.published_plans else None


def _solution_step(scenario: EvaluationScenario, step_index: int) -> SolutionStep | None:
    if 0 <= step_index < len(scenario.solution):
        return scenario.solution[step_index]
    return None


def _gains(step: SolutionStep) -> str:
    parts: list[str] = []
    if step.output.gained_permissions:
        parts.append("permissions " + ", ".join(f"`{p}`" for p in step.output.gained_permissions))
    if step.output.gained_capabilities:
        parts.append("capabilities " + ", ".join(f"`{c}`" for c in step.output.gained_capabilities))
    if step.output.discovered_resources:
        parts.append("resources " + ", ".join(f"`{r}`" for r in step.output.discovered_resources))
    return "; ".join(parts)


def _title(knowledge: Knowledge, technique_id: str) -> str:
    technique = knowledge.matrix.techniques.get(technique_id)
    return technique.title if technique is not None else technique_id
