"""Agentic (tool-driven) proposal loop.

Instead of being handed a fixed candidate slice, the model is given read-only tools over the
IAMouflage manual (see proposer/manual_tools.py) and decides for itself what to retrieve, given
the environment and objective. The loop has two phases so tools and structured output never
collide in one call:

  1. Exploration: repeated generate_content calls with the tools enabled; the model issues
     function calls, we execute them against the snapshot and feed the results back, until it
     stops calling tools or a tool-call budget is reached.
  2. Finalization: one more call WITHOUT tools but WITH the GeminiChoices JSON schema, forcing a
     valid ranked answer.

The model only ever learns real technique_ids from tool results, so its final choices resolve in
the snapshot. The deterministic validator, not this loop, decides feasibility and coverage.
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from typing import TYPE_CHECKING, Any

from google.genai import types

from core.metrics import TOKEN_KEYS, ProposalCost, tokens_from_usage
from core.models import MatrixSnapshot, ProposalRequest
from proposer.manual_tools import ManualQuery
from proposer.models import GeminiChoices

if TYPE_CHECKING:
    from proposer.gemini import GeminiProposer

AGENTIC_SYSTEM_INSTRUCTION = """You are the proposal component of a human-gated GCP IAM security
evaluation. You are given tools to search a manual of attack techniques and loaded detections.
Use them to discover which next techniques advance the objective from the current environment.
Only ever reference technique IDs that a tool returned; never invent IDs or permissions. Prefer
techniques whose footprint avoids the loaded detections, but the deterministic validator, not you,
decides feasibility and coverage. The user payload may include already_tried_technique_ids:
techniques the validator already rejected in earlier rounds of this step — do NOT propose any of
those again; find different ones. When you have gathered enough, stop calling tools and return a
ranked list of candidate technique IDs."""

_STRING = types.Schema(type=types.Type.STRING)
_INTEGER = types.Schema(type=types.Type.INTEGER)

_TOOL_DECLARATIONS = [
    types.FunctionDeclaration(
        name="search_techniques",
        description="Search the technique catalog by keywords/service/tactic/permission.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "keywords": _STRING,
                "service": _STRING,
                "tactic": _STRING,
                "permission": types.Schema(
                    type=types.Type.STRING,
                    description=(
                        "Return only techniques that REQUIRE this exact permission to run "
                        "(match against a permission the identity already holds)."
                    ),
                ),
                "limit": _INTEGER,
            },
        ),
    ),
    types.FunctionDeclaration(
        name="get_technique",
        description="Get one technique's required/footprint permissions by its id.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={"technique_id": _STRING},
            required=["technique_id"],
        ),
    ),
    types.FunctionDeclaration(
        name="search_detections",
        description="Search loaded detections by covered permission, service, or keywords.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "permission": _STRING,
                "service": _STRING,
                "keywords": _STRING,
                "limit": _INTEGER,
            },
        ),
    ),
    types.FunctionDeclaration(
        name="list_services",
        description="List the distinct services present in the technique catalog.",
        parameters=types.Schema(type=types.Type.OBJECT, properties={}),
    ),
]


def _serialize_parts(content: Any) -> list[dict[str, Any]]:
    """Faithfully render one Content's parts for the conversation log.

    Keeps thoughts, plain text, function calls and function responses distinct and untruncated —
    this is the record of what the model actually said and did, not a summary of it.
    """
    parts: list[dict[str, Any]] = []
    for part in getattr(content, "parts", None) or ():
        function_call = getattr(part, "function_call", None)
        if function_call is not None:
            parts.append(
                {
                    "type": "function_call",
                    "name": function_call.name,
                    "arguments": dict(function_call.args or {}),
                }
            )
            continue
        function_response = getattr(part, "function_response", None)
        if function_response is not None:
            parts.append(
                {
                    "type": "function_response",
                    "name": function_response.name,
                    "response": function_response.response,
                }
            )
            continue
        text = getattr(part, "text", None)
        if text:
            parts.append(
                {"type": "thought" if getattr(part, "thought", False) else "text", "text": text}
            )
    return parts


def _format_call(name: str, arguments: dict[str, Any]) -> str:
    """Render one tool call as `name(key=value, ...)` for the human-readable progress log."""
    rendered = []
    for key in sorted(arguments):
        value = arguments[key]
        if value in ("", None):
            continue
        text = str(value)
        if len(text) > 60:
            text = f"{text[:57]}..."
        rendered.append(f"{key}={text}")
    return f"{name}({', '.join(rendered)})"


def _describe_result(result: Any) -> str:
    """One-phrase description of what a tool returned, for the progress log."""
    if result is None:
        return "no match"
    if isinstance(result, list):
        identifiers = [
            str(item.get("technique_id") or item.get("rule_id") or "")
            for item in result
            if isinstance(item, dict)
        ]
        identifiers = [identifier for identifier in identifiers if identifier]
        if identifiers:
            shown = ", ".join(identifiers[:5])
            suffix = ", ..." if len(identifiers) > 5 else ""
            return f"{len(result)} result(s): {shown}{suffix}"
        return f"{len(result)} result(s)"
    if isinstance(result, dict):
        if "error" in result:
            return f"error: {result['error']}"
        identifier = result.get("technique_id") or result.get("rule_id")
        return f"1 result: {identifier}" if identifier else "1 result"
    return str(result)[:80]


def _dispatch(query: ManualQuery, name: str, arguments: dict[str, Any]) -> Any:
    if name == "search_techniques":
        return query.search_techniques(
            keywords=str(arguments.get("keywords", "")),
            service=str(arguments.get("service", "")),
            tactic=str(arguments.get("tactic", "")),
            permission=str(arguments.get("permission", "")),
            limit=int(arguments.get("limit", 20)),
        )
    if name == "get_technique":
        return query.get_technique(str(arguments.get("technique_id", "")))
    if name == "search_detections":
        return query.search_detections(
            permission=str(arguments.get("permission", "")),
            service=str(arguments.get("service", "")),
            keywords=str(arguments.get("keywords", "")),
            limit=int(arguments.get("limit", 20)),
        )
    if name == "list_services":
        return query.list_services()
    return {"error": f"unknown tool {name!r}"}


def _extract_choices(text: str) -> GeminiChoices:
    try:
        return GeminiChoices.model_validate_json(text)
    except ValueError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            return GeminiChoices.model_validate_json(text[start : end + 1])
        raise


def propose_with_tools(
    gemini: GeminiProposer, request: ProposalRequest, matrix: MatrixSnapshot
) -> GeminiChoices:
    if gemini.client is None:
        raise RuntimeError(gemini._client_error_message())
    gemini.last_proposal_cost = None
    started = time.monotonic()
    token_totals = {key: 0 for key in TOKEN_KEYS}
    query = ManualQuery(matrix)
    model = gemini.model_candidates[0]
    max_tool_calls = int(os.getenv("GEMINI_MAX_TOOL_CALLS", "8"))
    timeout_ms = int(gemini.timeout_seconds * 1000)

    user_payload = {
        "objective": request.objective,
        "identity": request.identity,
        "environment": request.environment_summary,
        "already_tried_technique_ids": list(request.excluded_technique_ids),
        "previous_rejections": request.previous_rejections,
        "maximum_choices": request.maximum_proposals,
    }
    contents: list[Any] = [
        types.Content(
            role="user",
            parts=[types.Part.from_text(text=json.dumps(user_payload, sort_keys=True))],
        )
    ]

    explore_config = types.GenerateContentConfig(
        system_instruction=AGENTIC_SYSTEM_INSTRUCTION,
        tools=[types.Tool(function_declarations=_TOOL_DECLARATIONS)],
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        thinking_config=types.ThinkingConfig(
            thinking_level=gemini.thinking_level,
            include_thoughts=True,
        ),
        http_options=types.HttpOptions(timeout=timeout_ms),
    )

    gemini._conversation(
        "session_started",
        mode="agentic",
        model=model,
        thinking_level=gemini.thinking_level,
        max_tool_calls=max_tool_calls,
        messages=[
            {"role": "system", "content": AGENTIC_SYSTEM_INSTRUCTION},
            {"role": "user", "content": user_payload},
        ],
        available_tools=[
            {"name": declaration.name, "description": declaration.description}
            for declaration in _TOOL_DECLARATIONS
        ],
    )

    calls = 0
    rounds = 0
    analysis = ""
    observations: list[dict[str, Any]] = []
    while rounds <= max_tool_calls:
        rounds += 1
        response = gemini.client.models.generate_content(
            model=model, contents=contents, config=explore_config
        )
        function_calls = list(getattr(response, "function_calls", None) or [])
        requested = [{"tool": fc.name, "arguments": dict(fc.args or {})} for fc in function_calls]
        candidate = response.candidates[0] if getattr(response, "candidates", None) else None
        candidate_content = getattr(candidate, "content", None)
        response_usage = gemini._usage(response)
        for key, value in tokens_from_usage(response_usage).items():
            token_totals[key] += value
        gemini._conversation(
            "model_turn",
            round=rounds,
            model=model,
            finish_reason=str(getattr(candidate, "finish_reason", None) or ""),
            messages=[{"role": "assistant", "parts": _serialize_parts(candidate_content)}],
            usage=response_usage,
        )
        gemini._emit(
            "agentic_round",
            round=rounds,
            tool_calls=requested,
            total_tool_calls=calls,
            summary=(
                f"round {rounds}: "
                + (
                    "; ".join(_format_call(call["tool"], call["arguments"]) for call in requested)
                    or "no tool calls (model is ready to answer)"
                )
            ),
        )
        if not function_calls or calls >= max_tool_calls:
            analysis = getattr(response, "text", None) or gemini._response_text(response) or ""
            gemini._conversation(
                "exploration_finished",
                round=rounds,
                total_tool_calls=calls,
                reason="tool_budget_reached" if function_calls else "model_stopped_calling_tools",
                analysis=analysis,
                thought_summaries=gemini._thought_summaries(response),
            )
            break
        if candidate_content is not None:
            contents.append(candidate_content)
        # One tool turn must carry exactly as many function_response parts as the model's turn
        # had function_call parts (the API rejects a mismatch), so batch them into one Content.
        response_parts = []
        for function_call in function_calls:
            calls += 1
            arguments = dict(function_call.args or {})
            result = _dispatch(query, function_call.name, arguments)
            observations.append(
                {"tool": function_call.name, "arguments": arguments, "result": result}
            )
            gemini._emit(
                "agentic_tool_call",
                round=rounds,
                call=calls,
                tool=function_call.name,
                arguments=arguments,
                summary=(
                    f"{_format_call(function_call.name, arguments)} -> {_describe_result(result)}"
                ),
            )
            response_parts.append(
                types.Part.from_function_response(
                    name=function_call.name, response={"result": result}
                )
            )
        contents.append(types.Content(role="tool", parts=response_parts))
        gemini._conversation(
            "tool_turn",
            round=rounds,
            total_tool_calls=calls,
            messages=[{"role": "tool", "parts": _serialize_parts(contents[-1])}],
        )

    # Finalize with a clean call: a fresh prompt carrying the tool observations and the model's
    # own analysis, plus the JSON schema. No function-call history is replayed, so controlled
    # generation (response schema) never has to coexist with tools in one request.
    finalize_payload = {
        "objective": request.objective,
        "identity": request.identity,
        "environment": request.environment_summary,
        "already_tried_technique_ids": list(request.excluded_technique_ids),
        "previous_rejections": request.previous_rejections,
        "maximum_choices": request.maximum_proposals,
        "analysis": analysis,
        "manual_observations": observations,
        "instruction": (
            "Return the final ranked technique choices as JSON. Use only technique IDs that "
            "appear in manual_observations, and never any id in already_tried_technique_ids."
        ),
    }
    final_contents = [
        types.Content(
            role="user",
            parts=[
                types.Part.from_text(text=json.dumps(finalize_payload, sort_keys=True, default=str))
            ],
        )
    ]
    final_config = types.GenerateContentConfig(
        system_instruction=AGENTIC_SYSTEM_INSTRUCTION,
        response_mime_type="application/json",
        response_json_schema=GeminiChoices.model_json_schema(),
        thinking_config=types.ThinkingConfig(
            thinking_level=gemini.thinking_level,
            include_thoughts=True,
        ),
        http_options=types.HttpOptions(timeout=timeout_ms),
    )
    gemini._conversation(
        "finalize_request",
        model=model,
        total_tool_calls=calls,
        messages=[
            {"role": "system", "content": AGENTIC_SYSTEM_INSTRUCTION},
            {"role": "user", "content": finalize_payload},
        ],
        response_schema=GeminiChoices.model_json_schema(),
    )
    final = gemini.client.models.generate_content(
        model=model, contents=final_contents, config=final_config
    )
    text = getattr(final, "text", None) or gemini._response_text(final)
    final_candidate = final.candidates[0] if getattr(final, "candidates", None) else None
    final_usage = gemini._usage(final)
    for key, value in tokens_from_usage(final_usage).items():
        token_totals[key] += value
    gemini._conversation(
        "finalize_response",
        model=model,
        finish_reason=str(getattr(final_candidate, "finish_reason", None) or ""),
        messages=[
            {
                "role": "assistant",
                "parts": _serialize_parts(getattr(final_candidate, "content", None)),
            }
        ],
        raw_response_text=text,
        thought_summaries=gemini._thought_summaries(final),
        usage=final_usage,
    )
    if not text:
        raise RuntimeError("agentic proposer returned an empty finalization response")
    choices = _extract_choices(text)
    gemini.last_model = model
    chosen = [choice.technique_id for choice in choices.choices]
    gemini._emit(
        "agentic_completed",
        model=model,
        total_tool_calls=calls,
        choices=chosen,
        summary=f"{calls} tool call(s) -> {', '.join(chosen) or 'no choices'}",
    )
    gemini._conversation(
        "session_completed",
        model=model,
        total_rounds=rounds,
        total_tool_calls=calls,
        decision_summary=choices.decision_summary,
        choices=choices.model_dump(mode="json"),
    )
    # api_calls = one model round-trip per exploration round plus the single finalize call.
    gemini.last_proposal_cost = ProposalCost(
        api_calls=rounds + 1,
        exploration_rounds=rounds,
        tool_calls=calls,
        tool_counts=dict(Counter(observation["tool"] for observation in observations)),
        tokens=token_totals,
        latency_seconds=round(time.monotonic() - started, 3),
    )
    return choices
