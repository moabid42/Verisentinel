from __future__ import annotations

from dataclasses import dataclass

from core.models import CandidateCard, DecisionKind


@dataclass(frozen=True, slots=True)
class TerminalChoice:
    decision: DecisionKind
    candidate_id: str | None = None
    reason: str = ""


def render_candidates(candidates: tuple[CandidateCard, ...]) -> str:
    if not candidates:
        return "\nNo admissible candidates are available in the current cycle."
    sections = ["\nValidated candidates (execution still requires your approval):"]
    for index, card in enumerate(candidates, start=1):
        proposal = card.proposal
        validation = card.validation
        sections.extend(
            (
                "",
                f"[{index}] {card.technique_title or proposal.technique_id}",
                f"    technique: {proposal.technique_id}",
                f"    action:    {proposal.action_id}",
                f"    target:    {proposal.target}",
                f"    required:  {', '.join(card.required_permissions) or '(none)'}",
                f"    uncovered: {', '.join(validation.uncovered_permissions) or '(none)'}",
                f"    grants:    {', '.join(card.expected_capabilities) or '(none)'}",
                f"    rationale: {proposal.rationale}",
                f"    validator: {validation.explanation}",
            )
        )
    return "\n".join(sections)


def parse_choice(raw: str, candidates: tuple[CandidateCard, ...]) -> TerminalChoice:
    value = raw.strip().lower()
    if value in {"q", "quit", "terminate"}:
        return TerminalChoice(DecisionKind.TERMINATE)
    if value in {"a", "alternatives"}:
        return TerminalChoice(DecisionKind.REQUEST_ALTERNATIVES)
    if value in {"x", "reject-all"}:
        return TerminalChoice(DecisionKind.REJECT_ALL)

    decision = DecisionKind.APPROVE
    number = value
    if value.startswith("r"):
        decision = DecisionKind.REJECT
        number = value[1:].strip()
    if not number.isdigit():
        raise ValueError("enter 1-3, r1-r3, a, x, or q")
    index = int(number) - 1
    if index < 0 or index >= len(candidates):
        raise ValueError("the selected candidate number is not displayed")
    return TerminalChoice(decision, candidates[index].proposal.candidate_id)
