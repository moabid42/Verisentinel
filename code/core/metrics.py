"""Cost metrics for a run: what each proposer call cost, aggregated per solution step.

The proposer produces one ``ProposalCost`` per ``propose()`` call (model round-trips, tool calls,
tokens, latency). The orchestrator folds those into a ``StepMetrics`` per state (solution step),
and ``RunMetrics.summary()`` renders per-state rows plus run totals/averages for the report. These
numbers exist to answer "which states are expensive, and can we reach the answer with fewer calls?"
"""

from __future__ import annotations

from dataclasses import dataclass, field

TOKEN_KEYS = ("input", "output", "thought", "tool_use", "total")

# Maps the raw keys returned by GeminiProposer._usage() onto the compact names used here.
_USAGE_KEY_MAP = {
    "total_input_tokens": "input",
    "total_output_tokens": "output",
    "total_thought_tokens": "thought",
    "total_tool_use_tokens": "tool_use",
    "total_tokens": "total",
}


def tokens_from_usage(usage: dict[str, int | None] | None) -> dict[str, int]:
    """Normalise a ``_usage()`` dict into ``{input, output, thought, tool_use, total}`` ints.

    Missing or ``None`` values become 0, so summing across calls is always well defined.
    """
    totals = {key: 0 for key in TOKEN_KEYS}
    for raw_key, value in (usage or {}).items():
        compact = _USAGE_KEY_MAP.get(raw_key)
        if compact is not None and value:
            totals[compact] += int(value)
    return totals


@dataclass(frozen=True, slots=True)
class ProposalCost:
    """The cost of a single ``propose()`` call."""

    api_calls: int = 0
    exploration_rounds: int = 0
    tool_calls: int = 0
    tool_counts: dict[str, int] = field(default_factory=dict)
    tokens: dict[str, int] = field(default_factory=lambda: {key: 0 for key in TOKEN_KEYS})
    latency_seconds: float = 0.0

    @classmethod
    def zero(cls) -> ProposalCost:
        return cls()

    def __add__(self, other: ProposalCost) -> ProposalCost:
        tool_counts = dict(self.tool_counts)
        for name, count in other.tool_counts.items():
            tool_counts[name] = tool_counts.get(name, 0) + count
        tokens = {key: self.tokens.get(key, 0) + other.tokens.get(key, 0) for key in TOKEN_KEYS}
        return ProposalCost(
            api_calls=self.api_calls + other.api_calls,
            exploration_rounds=self.exploration_rounds + other.exploration_rounds,
            tool_calls=self.tool_calls + other.tool_calls,
            tool_counts=tool_counts,
            tokens=tokens,
            latency_seconds=round(self.latency_seconds + other.latency_seconds, 6),
        )


@dataclass(slots=True)
class StepMetrics:
    """Accumulated cost and candidate counts for one solution step (state)."""

    step_index: int
    proposal_rounds: int = 0
    proposed: int = 0
    accepted: int = 0
    rejected: int = 0
    cost: ProposalCost = field(default_factory=ProposalCost.zero)

    def add_cost(self, cost: ProposalCost | None) -> None:
        self.cost = self.cost + (cost or ProposalCost.zero())

    def to_dict(self) -> dict:
        return {
            "step_index": self.step_index,
            "proposal_rounds": self.proposal_rounds,
            "model_calls": self.cost.api_calls,
            "exploration_rounds": self.cost.exploration_rounds,
            "tool_calls": self.cost.tool_calls,
            "tool_counts": dict(sorted(self.cost.tool_counts.items())),
            "candidates": {
                "proposed": self.proposed,
                "accepted": self.accepted,
                "rejected": self.rejected,
            },
            "tokens": dict(self.cost.tokens),
            "latency_seconds": round(self.cost.latency_seconds, 3),
        }


class RunMetrics:
    """Ordered per-step metrics for a run, plus a report-ready ``summary()``."""

    def __init__(self) -> None:
        self.steps: list[StepMetrics] = []

    def open_step(self, step_index: int) -> StepMetrics:
        step = StepMetrics(step_index=step_index)
        self.steps.append(step)
        return step

    def summary(self) -> dict:
        per_state = [step.to_dict() for step in self.steps]
        state_count = len(per_state)
        total_cost = ProposalCost.zero()
        proposal_rounds = proposed = accepted = rejected = 0
        for step in self.steps:
            total_cost = total_cost + step.cost
            proposal_rounds += step.proposal_rounds
            proposed += step.proposed
            accepted += step.accepted
            rejected += step.rejected

        totals = {
            "states": state_count,
            "proposal_rounds": proposal_rounds,
            "model_calls": total_cost.api_calls,
            "exploration_rounds": total_cost.exploration_rounds,
            "tool_calls": total_cost.tool_calls,
            "tool_counts": dict(sorted(total_cost.tool_counts.items())),
            "candidates": {"proposed": proposed, "accepted": accepted, "rejected": rejected},
            "tokens": dict(total_cost.tokens),
            "latency_seconds": round(total_cost.latency_seconds, 3),
        }

        def per_state_mean(value: float) -> float:
            return round(value / state_count, 3) if state_count else 0.0

        averages = {
            "proposal_rounds": per_state_mean(proposal_rounds),
            "model_calls": per_state_mean(total_cost.api_calls),
            "tool_calls": per_state_mean(total_cost.tool_calls),
            "total_tokens": per_state_mean(total_cost.tokens["total"]),
            "latency_seconds": per_state_mean(total_cost.latency_seconds),
        }
        return {"per_state": per_state, "totals": totals, "averages": averages}
