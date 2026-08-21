from __future__ import annotations

import json
import os
import time
from queue import Empty, Queue
from threading import Thread
from typing import Any

import httpx
from google import genai
from google.genai import types

from core.metrics import ProposalCost, tokens_from_usage
from core.models import MatrixSnapshot, ProposalRequest, TechniqueDefinition
from core.tracing import DebugTrace
from proposer.models import GeminiChoices

SYSTEM_INSTRUCTION = """You are the proposal component of a human-gated GCP IAM security
evaluation. Select only technique IDs from allowed_techniques. Do not invent permissions,
targets, commands, or identifiers. Rank useful next techniques for the stated objective.
Return a short decision_summary that explains the ranking factors at a high level; do not reveal
private chain-of-thought. The deterministic validator, not you, decides feasibility and detection
coverage. An external operator or deterministic evaluation approval policy, not you, decides
whether any validated action is executed."""


class GeminiProposer:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        *,
        client: Any | None = None,
        trace: DebugTrace | None = None,
        conversation_trace: DebugTrace | None = None,
        timeout_seconds: float | None = None,
        maximum_attempts: int | None = None,
        thinking_level: str | None = None,
        api_mode: str | None = None,
        fallback_models: tuple[str, ...] | None = None,
        heartbeat_seconds: float | None = None,
        use_vertex: bool | None = None,
        vertex_project: str | None = None,
        vertex_location: str | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.getenv("GEMINI_API_KEY")
        # Backend selection. False -> Google AI Studio (Gemini Developer API) via an
        # API key (free tier). True -> Vertex AI on a GCP project via ADC, so usage
        # bills to that project (and is covered by the Cloud free-trial credit).
        self.use_vertex = (
            use_vertex
            if use_vertex is not None
            else os.getenv("GEMINI_USE_VERTEX", "false").lower() == "true"
        )
        self.vertex_project = (
            vertex_project or os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GEMINI_PROJECT")
        )
        self.vertex_location = (
            vertex_location
            or os.getenv("GOOGLE_CLOUD_LOCATION")
            or os.getenv("GEMINI_LOCATION", "global")
        )
        self.model = model or os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
        configured_fallbacks = fallback_models
        if configured_fallbacks is None:
            configured_fallbacks = tuple(
                item.strip()
                for item in os.getenv(
                    "GEMINI_FALLBACK_MODELS",
                    "gemini-3.6-flash,gemini-3.1-flash-lite",
                ).split(",")
                if item.strip()
            )
        self.model_candidates = tuple(dict.fromkeys((self.model, *configured_fallbacks)))
        self.last_model = self.model
        # Cost of the most recent propose()/propose_with_tools() call, read by the orchestrator
        # to aggregate per-state metrics. None until the first successful call.
        self.last_proposal_cost: ProposalCost | None = None
        self.trace = trace
        self.conversation_trace = conversation_trace
        self.timeout_seconds = timeout_seconds or float(os.getenv("GEMINI_TIMEOUT_SECONDS", "120"))
        self.maximum_attempts = maximum_attempts or int(os.getenv("GEMINI_MAX_ATTEMPTS", "3"))
        self.thinking_level = thinking_level or os.getenv("GEMINI_THINKING_LEVEL", "medium")
        self.heartbeat_seconds = heartbeat_seconds or float(
            os.getenv("GEMINI_HEARTBEAT_SECONDS", "10")
        )
        self.api_mode = api_mode or os.getenv("GEMINI_API_MODE", "generate_content")
        if self.maximum_attempts < 1:
            raise ValueError("GEMINI_MAX_ATTEMPTS must be at least 1")
        if self.thinking_level not in {"minimal", "low", "medium", "high"}:
            raise ValueError("GEMINI_THINKING_LEVEL must be minimal, low, medium, or high")
        if self.heartbeat_seconds <= 0:
            raise ValueError("GEMINI_HEARTBEAT_SECONDS must be greater than zero")
        if self.api_mode not in {"generate_content", "interactions"}:
            raise ValueError("GEMINI_API_MODE must be generate_content or interactions")
        if (
            client is not None
            and self.api_mode == "generate_content"
            and not hasattr(client, "models")
            and hasattr(client, "interactions")
        ):
            self.api_mode = "interactions"
        self.force_ipv4 = os.getenv("GEMINI_FORCE_IPV4", "true").lower() == "true"
        self._external_client = client is not None
        self._http_client: httpx.Client | None = None
        self.client = client or self._create_client()

    @property
    def configured(self) -> bool:
        return self.client is not None

    def propose_with_tools(self, request: ProposalRequest, matrix: MatrixSnapshot) -> GeminiChoices:
        """Agentic proposal: the model retrieves from the manual with tools (see proposer.agentic),
        used by IAMouflage mode instead of being handed a fixed candidate slice."""
        from proposer.agentic import propose_with_tools

        return propose_with_tools(self, request, matrix)

    def propose(
        self,
        request: ProposalRequest,
        candidates: tuple[TechniqueDefinition, ...],
        matrix: MatrixSnapshot,
    ) -> GeminiChoices:
        if self.client is None:
            raise RuntimeError(self._client_error_message())
        self.last_proposal_cost = None
        permission_names = matrix.permissions
        catalog = [
            {
                "technique_id": technique.technique_id,
                "title": technique.title,
                "tactic": technique.tactic,
                "service": technique.service,
                "required_permissions": [
                    permission_names[index] for index in technique.required_indices
                ],
                "footprint_permissions": [
                    permission_names[index] for index in technique.footprint_indices
                ],
                "expected_capabilities": list(technique.grants),
            }
            for technique in candidates
        ]
        prompt = {
            "objective": request.objective,
            "identity": request.identity,
            "environment": request.environment_summary,
            "previous_rejections": request.previous_rejections,
            "maximum_choices": request.maximum_proposals,
            "allowed_techniques": catalog,
        }
        prompt_text = json.dumps(prompt, sort_keys=True)
        self._emit(
            "request_prepared",
            model=self.model,
            model_candidates=self.model_candidates,
            thinking_level=self.thinking_level,
            force_ipv4=self.force_ipv4,
            api_mode=self.api_mode,
            timeout_seconds=self.timeout_seconds,
            candidate_count=len(catalog),
            prompt=prompt,
        )
        self._conversation(
            "request_prepared",
            model=self.model,
            api_mode=self.api_mode,
            messages=[
                {"role": "system", "content": SYSTEM_INSTRUCTION},
                {"role": "user", "content": prompt},
            ],
            response_schema=GeminiChoices.model_json_schema(),
            thinking_level=self.thinking_level,
        )

        for attempt in range(1, self.maximum_attempts + 1):
            attempt_model = self.model_candidates[min(attempt - 1, len(self.model_candidates) - 1)]
            started = time.monotonic()
            self._emit(
                "request_started",
                attempt=attempt,
                maximum_attempts=self.maximum_attempts,
                model=attempt_model,
            )
            self._conversation(
                "request_started",
                model=attempt_model,
                api_mode=self.api_mode,
                attempt=attempt,
                maximum_attempts=self.maximum_attempts,
                timeout_seconds=self.timeout_seconds,
            )
            try:
                interaction = self._request_with_deadline(prompt_text, attempt, model=attempt_model)
                response_text = self._response_text(interaction)
                if not response_text:
                    raise RuntimeError("Gemini returned an empty proposal response")
                choices = GeminiChoices.model_validate_json(response_text)
                thought_summaries = self._thought_summaries(interaction)
                usage = self._usage(interaction)
                self.last_proposal_cost = ProposalCost(
                    api_calls=attempt,
                    tokens=tokens_from_usage(usage),
                    latency_seconds=round(time.monotonic() - started, 3),
                )
                self._emit(
                    "request_completed",
                    model=attempt_model,
                    attempt=attempt,
                    elapsed_seconds=round(time.monotonic() - started, 3),
                    interaction_id=getattr(interaction, "id", None),
                    status=getattr(interaction, "status", None),
                    thought_summaries=thought_summaries,
                    usage=usage,
                    decision_summary=choices.decision_summary,
                    response=choices.model_dump(mode="json"),
                )
                self._conversation(
                    "response_completed",
                    model=attempt_model,
                    attempt=attempt,
                    interaction_id=getattr(interaction, "id", None),
                    status=getattr(interaction, "status", None),
                    messages=[
                        {
                            "role": "assistant",
                            "content": choices.model_dump(mode="json"),
                        }
                    ],
                    raw_response_text=response_text,
                    thought_summaries=thought_summaries,
                    usage=usage,
                    elapsed_seconds=round(time.monotonic() - started, 3),
                )
                self.last_model = attempt_model
                return choices
            except Exception as error:
                self._emit(
                    "request_failed",
                    level="error",
                    model=attempt_model,
                    attempt=attempt,
                    elapsed_seconds=round(time.monotonic() - started, 3),
                    error_type=type(error).__name__,
                    error=str(error),
                    will_retry=attempt < self.maximum_attempts,
                )
                self._conversation(
                    "request_failed",
                    level="error",
                    model=attempt_model,
                    attempt=attempt,
                    error_type=type(error).__name__,
                    error=str(error),
                    elapsed_seconds=round(time.monotonic() - started, 3),
                    will_retry=attempt < self.maximum_attempts,
                )
                if attempt == self.maximum_attempts:
                    raise RuntimeError(
                        f"Gemini proposal failed after {self.maximum_attempts} attempt(s)"
                    ) from error
                time.sleep(min(2 ** (attempt - 1), 4))
        raise AssertionError("unreachable Gemini retry state")

    def _emit(self, event: str, *, level: str = "info", **details: Any) -> None:
        if self.trace is not None:
            self.trace.emit("gemini", event, level=level, **details)

    def _conversation(self, event: str, *, level: str = "info", **details: Any) -> None:
        if self.conversation_trace is not None:
            self.conversation_trace.emit("conversation", event, level=level, **details)

    def _request_with_deadline(
        self, prompt_text: str, attempt: int, *, model: str | None = None
    ) -> Any:
        if self.client is None:
            raise RuntimeError("Gemini client is not configured")
        completed: Queue[tuple[bool, Any]] = Queue(maxsize=1)
        request_model = model or self.model

        def request() -> None:
            try:
                interaction = self._request_once(prompt_text, model=request_model)
                completed.put((True, interaction))
            except BaseException as error:
                completed.put((False, error))

        worker = Thread(
            target=request,
            name=f"gemini-request-{attempt}",
            daemon=True,
        )
        worker.start()
        started = time.monotonic()
        deadline = started + self.timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._emit(
                    "request_deadline_exceeded",
                    level="error",
                    attempt=attempt,
                    model=request_model,
                    timeout_seconds=self.timeout_seconds,
                )
                self._conversation(
                    "request_deadline_exceeded",
                    level="error",
                    model=request_model,
                    attempt=attempt,
                    timeout_seconds=self.timeout_seconds,
                )
                self._reset_client_after_timeout()
                raise TimeoutError(
                    f"Gemini request exceeded the hard {self.timeout_seconds:g}s deadline"
                )
            try:
                succeeded, value = completed.get(timeout=min(self.heartbeat_seconds, remaining))
            except Empty:
                elapsed = round(time.monotonic() - started, 1)
                self._emit(
                    "request_heartbeat",
                    attempt=attempt,
                    model=request_model,
                    elapsed_seconds=elapsed,
                    timeout_seconds=self.timeout_seconds,
                )
                continue
            if succeeded:
                return value
            if isinstance(value, Exception):
                raise value
            raise RuntimeError(f"Gemini request aborted with {type(value).__name__}")

    def _request_once(self, prompt_text: str, *, model: str | None = None) -> Any:
        if self.client is None:
            raise RuntimeError("Gemini client is not configured")
        request_model = model or self.model
        if self.api_mode == "interactions":
            return self.client.interactions.create(
                model=request_model,
                input=prompt_text,
                system_instruction=SYSTEM_INSTRUCTION,
                generation_config={
                    "thinking_level": self.thinking_level,
                    "thinking_summaries": "auto",
                },
                response_format={
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": GeminiChoices.model_json_schema(),
                },
                store=False,
                timeout=self.timeout_seconds,
            )
        return self.client.models.generate_content(
            model=request_model,
            contents=prompt_text,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                thinking_config=types.ThinkingConfig(
                    thinking_level=self.thinking_level,
                    include_thoughts=True,
                ),
                response_mime_type="application/json",
                response_json_schema=GeminiChoices.model_json_schema(),
                http_options=types.HttpOptions(timeout=int(self.timeout_seconds * 1000)),
            ),
        )

    def _reset_client_after_timeout(self) -> None:
        if self._http_client is not None:
            stale_http_client = self._http_client
            self._http_client = None
            Thread(
                target=stale_http_client.close,
                name="gemini-stale-client-close",
                daemon=True,
            ).start()
        if not self._external_client:
            self.client = self._create_client()

    def _client_error_message(self) -> str:
        if self.use_vertex:
            return (
                "Vertex AI mode is on but no project is set; export GOOGLE_CLOUD_PROJECT "
                "(or pass vertex_project) and run `gcloud auth application-default login`"
            )
        return "GEMINI_API_KEY is required; no deterministic fallback is enabled"

    def _create_client(self) -> Any | None:
        http_options: types.HttpOptions | None = None
        if self.force_ipv4:
            self._http_client = httpx.Client(
                transport=httpx.HTTPTransport(local_address="0.0.0.0"),
                timeout=self.timeout_seconds,
            )
            http_options = types.HttpOptions(httpx_client=self._http_client)
        if self.use_vertex:
            if not self.vertex_project:
                return None
            return genai.Client(
                vertexai=True,
                project=self.vertex_project,
                location=self.vertex_location,
                http_options=http_options,
            )
        if not self.api_key:
            return None
        return genai.Client(api_key=self.api_key, http_options=http_options)

    @staticmethod
    def _thought_summaries(interaction: Any) -> list[str]:
        summaries: list[str] = []
        for candidate in getattr(interaction, "candidates", None) or ():
            content = getattr(candidate, "content", None)
            for part in getattr(content, "parts", None) or ():
                if getattr(part, "thought", False) and getattr(part, "text", None):
                    summaries.append(part.text)
        items = getattr(interaction, "steps", None) or getattr(interaction, "outputs", None) or ()
        for item in items:
            if getattr(item, "type", None) != "thought":
                continue
            for block in getattr(item, "summary", None) or ():
                text = getattr(block, "text", None)
                if text:
                    summaries.append(text)
        return summaries

    @staticmethod
    def _response_text(interaction: Any) -> str:
        response_text = getattr(interaction, "text", None)
        if response_text:
            return response_text
        output_text = getattr(interaction, "output_text", None)
        if output_text:
            return output_text
        items = getattr(interaction, "steps", None) or getattr(interaction, "outputs", None) or ()
        for item in reversed(items):
            if getattr(item, "type", None) == "text" and getattr(item, "text", None):
                return item.text
            if getattr(item, "type", None) != "model_output":
                continue
            for block in reversed(getattr(item, "content", None) or ()):
                if getattr(block, "type", None) == "text" and getattr(block, "text", None):
                    return block.text
        return ""

    @staticmethod
    def _usage(interaction: Any) -> dict[str, int | None]:
        usage = getattr(interaction, "usage", None)
        if usage is not None:
            names = (
                "total_input_tokens",
                "total_output_tokens",
                "total_thought_tokens",
                "total_tool_use_tokens",
                "total_tokens",
            )
            return {name: getattr(usage, name, None) for name in names}
        metadata = getattr(interaction, "usage_metadata", None)
        if metadata is None:
            return {}
        return {
            "total_input_tokens": getattr(metadata, "prompt_token_count", None),
            "total_output_tokens": getattr(metadata, "candidates_token_count", None),
            "total_thought_tokens": getattr(metadata, "thoughts_token_count", None),
            "total_tool_use_tokens": getattr(metadata, "tool_use_prompt_token_count", None),
            "total_tokens": getattr(metadata, "total_token_count", None),
        }
