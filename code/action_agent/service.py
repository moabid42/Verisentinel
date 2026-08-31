"""Model-assisted conversation for one pending registered action."""

from __future__ import annotations

import ast
import hashlib
import json
from typing import Any, Protocol

from google.genai import types

from action_agent.models import AuthoredAction
from core.models import (
    ActionArtifact,
    ActionAuthorRequest,
    ActionCommand,
    CandidateCard,
)

ACTION_QUESTION_INSTRUCTION = """You are explaining one pending, validated action to its
operator. Answer the operator's question directly and concisely using only the supplied action
metadata. Clearly distinguish a preinstalled tool from a file generated during this run. You may
recommend inspection, rejection, or approval, but you cannot modify files, approve the action, or
execute it. Never imply that a side effect already happened when current_state says it did not."""

ACTION_AUTHOR_INSTRUCTION = """You are the implementation agent inside a human-gated security
evaluation harness. Author exactly one complete Python file named action.py for the already
selected and validated action. Do not claim the file exists or that anything executed. The file
will run later in a read-only, non-root capsule with an internal network only. It must read the
JSON execution specification from /run/verisentinel/spec.json and the short-lived credential
from /run/verisentinel/credential, POST the exact typed action envelope to
http://verisentinel-mock:8080/execute with a Bearer authorization header, and write only the
gateway's JSON response to stdout. Use only json, pathlib, sys, urllib.error, and urllib.request
from the standard library. Do not invoke a shell, subprocess, dynamic code, or any other endpoint.
Return the entire file through the response schema. The harness, not you, assigns identifiers,
validates and writes the file, approves the command, or executes it."""

_ALLOWED_IMPORTS = {"json", "pathlib", "sys", "urllib.error", "urllib.request"}
_REQUIRED_SOURCE_VALUES = {
    "/run/verisentinel/spec.json",
    "/run/verisentinel/credential",
    "http://verisentinel-mock:8080/execute",
}
_DENIED_CALLS = {"__import__", "compile", "eval", "exec"}


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

    def author(self, request: ActionAuthorRequest) -> ActionCommand:
        """Propose and validate one model-authored file without writing it."""
        if self.gemini.client is None:
            raise RuntimeError("Gemini is not configured")
        model = self.gemini.model_candidates[0]
        prompt = request.model_dump(mode="json")
        self.gemini._emit(
            "action_authoring_started",
            summary=(
                f"Asking {model} to author one proposed action.py file; no file "
                "is being written and no command is executing."
            ),
            model=model,
            action_id=request.action_id,
        )
        response = self.gemini.client.models.generate_content(
            model=model,
            contents=json.dumps(prompt, sort_keys=True),
            config=types.GenerateContentConfig(
                system_instruction=ACTION_AUTHOR_INSTRUCTION,
                thinking_config=types.ThinkingConfig(
                    thinking_level=self.gemini.thinking_level,
                    include_thoughts=False,
                ),
                response_mime_type="application/json",
                response_json_schema=AuthoredAction.model_json_schema(),
                max_output_tokens=8192,
                http_options=types.HttpOptions(
                    timeout=int(self.gemini.timeout_seconds * 1000)
                ),
            ),
        )
        response_text = getattr(response, "text", None) or self.gemini._response_text(
            response
        )
        if not response_text:
            raise RuntimeError("Gemini returned an empty authored action")
        authored = AuthoredAction.model_validate_json(response_text)
        _validate_source(authored.file_path, authored.file_content)
        digest = "sha256:" + hashlib.sha256(
            authored.file_content.encode("utf-8")
        ).hexdigest()
        artifact = ActionArtifact(
            content=authored.file_content,
            digest=digest,
            source_model=model,
            rationale=authored.explanation,
        )
        command = _command(request, artifact)
        self.gemini._emit(
            "action_authoring_completed",
            summary=(
                f"{model} proposed action.py ({len(artifact.content)} characters); "
                "it remains unwritten and unexecuted."
            ),
            model=model,
            action_id=request.action_id,
            artifact_digest=artifact.digest,
        )
        self.gemini._conversation(
            "action_authoring_completed",
            model=model,
            messages=[
                {"role": "system", "content": ACTION_AUTHOR_INSTRUCTION},
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": authored.model_dump(mode="json")},
            ],
            usage=self.gemini._usage(response),
        )
        return command

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


def _validate_source(path: str, content: str) -> None:
    """Reject source outside the narrow capsule action contract."""
    if path != "action.py":
        raise ValueError("the model must propose exactly action.py")
    try:
        tree = ast.parse(content, filename=path)
        compile(tree, path, "exec")
    except (SyntaxError, ValueError):
        raise ValueError("the proposed action.py is not valid Python") from None
    imports: set[str] = set()
    calls: set[str] = set()
    literals: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add(node.module or "")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            calls.add(node.func.id)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            literals.add(node.value)
    if imports - _ALLOWED_IMPORTS:
        raise ValueError("the proposed action.py imports an unsupported module")
    if calls.intersection(_DENIED_CALLS):
        raise ValueError("the proposed action.py uses dynamic code execution")
    if not _REQUIRED_SOURCE_VALUES.issubset(literals):
        raise ValueError("the proposed action.py does not implement the capsule contract")


def _command(
    request: ActionAuthorRequest,
    artifact: ActionArtifact,
) -> ActionCommand:
    """Bind model source to the one registered typed operation."""
    parts = request.target.split("/")
    if (
        len(parts) != 4
        or parts[0] != "projects"
        or parts[2] != "buckets"
    ):
        raise ValueError("the selected action target is not a supported bucket")
    object_name = f"actions/{request.approval_id}.json"
    input_preview = json.dumps(
        {
            "action_id": request.action_id,
            "approval_id": request.approval_id,
            "engagement_id": request.engagement_id,
            "expected_capabilities": list(request.expected_capabilities),
            "identity": request.identity,
            "observed_permission_footprint": list(request.observed_permissions),
            "operation": "catalog.technique",
            "parameters": {},
            "target": request.target,
        },
        indent=2,
        sort_keys=True,
    )
    return ActionCommand(
        action_id=request.action_id,
        approval_id=request.approval_id,
        display="/usr/local/bin/python /workspace/action.py",
        prepared_by="model",
        input_summary=(
            "The approved typed action input and short-lived credential will be "
            "mounted only inside the controlled capsule after command approval."
        ),
        input_preview=input_preview,
        tool_source="action.py",
        tool_installation=(
            f"Proposed by {artifact.source_model}; not written until file approval."
        ),
        side_effects=(f"Create gs://{parts[3]}/{object_name}",),
        artifact=artifact,
    )
