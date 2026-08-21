"""Aggregation of per-call proposer cost into per-state and run-total metrics."""

from core.metrics import ProposalCost, RunMetrics, tokens_from_usage


def _cost(**tokens: int) -> ProposalCost:
    return ProposalCost(
        api_calls=2,
        exploration_rounds=1,
        tool_calls=3,
        tool_counts={"search_techniques": 2, "get_technique": 1},
        tokens={
            "input": tokens.get("input", 0),
            "output": 0,
            "thought": 0,
            "tool_use": 0,
            "total": tokens.get("total", 0),
        },
        latency_seconds=1.5,
    )


def test_tokens_from_usage_maps_and_zero_fills_none() -> None:
    usage = {"total_input_tokens": 100, "total_output_tokens": None, "total_tokens": 125}
    tokens = tokens_from_usage(usage)
    assert tokens == {"input": 100, "output": 0, "thought": 0, "tool_use": 0, "total": 125}


def test_proposal_cost_add_is_elementwise() -> None:
    combined = _cost(input=100, total=125) + _cost(input=50, total=75)
    assert combined.api_calls == 4
    assert combined.tool_calls == 6
    assert combined.tool_counts == {"search_techniques": 4, "get_technique": 2}
    assert combined.tokens["total"] == 200
    assert combined.latency_seconds == 3.0


def test_run_metrics_summary_totals_and_averages() -> None:
    metrics = RunMetrics()
    step0 = metrics.open_step(0)
    step0.proposal_rounds = 2
    step0.proposed, step0.accepted, step0.rejected = 3, 2, 1
    step0.add_cost(_cost(input=100, total=125))
    step0.add_cost(_cost(input=100, total=125))

    step1 = metrics.open_step(1)
    step1.proposal_rounds = 1
    step1.proposed, step1.accepted = 1, 1
    step1.add_cost(_cost(input=100, total=125))

    summary = metrics.summary()
    assert [state["step_index"] for state in summary["per_state"]] == [0, 1]
    assert summary["per_state"][0]["tokens"]["total"] == 250
    assert summary["per_state"][0]["tool_calls"] == 6

    totals = summary["totals"]
    assert totals["states"] == 2
    assert totals["proposal_rounds"] == 3
    assert totals["model_calls"] == 6  # 2 + 2 + 2 api_calls
    assert totals["tokens"]["total"] == 375
    assert totals["candidates"] == {"proposed": 4, "accepted": 3, "rejected": 1}

    averages = summary["averages"]
    assert averages["proposal_rounds"] == 1.5
    assert averages["total_tokens"] == 187.5


def test_run_metrics_summary_is_empty_when_no_states() -> None:
    summary = RunMetrics().summary()
    assert summary["per_state"] == []
    assert summary["totals"]["states"] == 0
    assert summary["averages"]["proposal_rounds"] == 0.0
