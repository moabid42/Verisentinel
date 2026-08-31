"""Model-assisted conversation for one pending registered action."""

from __future__ import annotations

import json
from typing import Any, Protocol

from google.genai import types

from core.models import CandidateCard

ACTION_QUESTION_INSTRUCTION = """You are explaining one pending, validated action to its
operator. Answer the operator's question directly and concisely using only the supplied action
metadata. Clearly distinguish a preinstalled tool from a file generated during this run. You may
recommend inspection, rejection, or approval, but you cannot modify files, approve the action, or
execute it. Never imply that a side effect already happened when current_state says it did not."""


class GeminiGateway(Protocol):
    """Gemini behavior used by the interactive action assistant."""

    client: Any
    model_candidates: tuple[str, ...]
    thinking_level: str
    timeout_seconds: float

    def _emit(self, event: str, *, level: str = "info", **details: Any) -> None: ...

    def _conversation(
        self,
        event: str,
        *,
        level: str = "info",
        **details: Any,
    ) -> None: ...

    def _response_text(self, interaction: Any) -> str: ...

    def _thought_summaries(self, interaction: Any) -> list[str]: ...

    def _usage(self, interaction: Any) -> dict[str, int | None]: ...


class ActionAgentService:
    """Answer operator messages without crossing the execution boundary."""

    def __init__(self, gemini: GeminiGateway) -> None:
        self.gemini = gemini

    def answer(self, card: CandidateCard, question: str) -> str:
        """Return a bounded explanatory turn for one pending action."""
        command = card.action_command
        if command is None:
            raise ValueError("action question requires a command preview")
        if self.gemini.client is None:
            raise RuntimeError("Gemini is not configured")
        prompt = {
            "question": question,
            "current_state": "not_executed",
            "technique_id": card.proposal.technique_id,
            "rationale": card.proposal.rationale,
            "command": command.model_dump(mode="json"),
        }
        model = self.gemini.model_candidates[0]
        self.gemini._emit(
            "action_question_started",
            summary=f"Asking {model} about the pending action; no action is executing.",
            model=model,
            question=question,
        )
        response = self.gemini.client.models.generate_content(
            model=model,
            contents=json.dumps(prompt, sort_keys=True),
            config=types.GenerateContentConfig(
                system_instruction=ACTION_QUESTION_INSTRUCTION,
                thinking_config=types.ThinkingConfig(
                    thinking_level=self.gemini.thinking_level,
                    include_thoughts=False,
                ),
                max_output_tokens=4096,
                http_options=types.HttpOptions(
                    timeout=int(self.gemini.timeout_seconds * 1000)
                ),
            ),
        )
        answer = (
            getattr(response, "text", None)
            or self.gemini._response_text(response)
        ).strip()
        if not answer:
            raise RuntimeError("Gemini returned an empty action explanation")
        answer = answer[:4096]
        self.gemini._emit(
            "action_question_completed",
            summary=answer,
            model=model,
        )
        self.gemini._conversation(
            "action_question_completed",
            model=model,
            messages=[
                {"role": "system", "content": ACTION_QUESTION_INSTRUCTION},
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": answer},
            ],
            thought_summaries=self.gemini._thought_summaries(response),
            usage=self.gemini._usage(response),
        )
        return answer
