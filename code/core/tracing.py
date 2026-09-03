from __future__ import annotations

import json
import sys
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any

SENSITIVE_KEY_PARTS = (
    "access_token",
    "api_key",
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
)
SAFE_TOKEN_COUNT_KEYS = {
    "total_input_tokens",
    "total_output_tokens",
    "total_thought_tokens",
    "total_tool_use_tokens",
    "total_tokens",
}
MAXIMUM_PROGRESS_SUMMARY_CHARACTERS = 500


class DebugTrace:
    """Append-only structured diagnostics with automatic secret redaction."""

    def __init__(
        self,
        path: Path,
        run_id: str,
        *,
        secrets: tuple[str, ...] = (),
        echo: bool = True,
        progress_path: Path | None = None,
    ) -> None:
        self.path = path
        self.run_id = run_id
        self.secrets = tuple(secret for secret in secrets if secret)
        self.echo = echo
        self.progress_path = progress_path
        self._lock = RLock()

    def emit(
        self,
        component: str,
        event: str,
        *,
        level: str = "info",
        summary: str | None = None,
        **details: Any,
    ) -> None:
        if summary is not None:
            details["summary"] = summary
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "run_id": self.run_id,
            "level": level,
            "component": component,
            "event": event,
            "details": self._redact(details),
        }
        line = json.dumps(record, sort_keys=True, default=str)
        progress_line = f"[{record['timestamp']}] {level.upper():5} {component}: {event}"
        if summary is not None:
            redacted_summary = self._redact(summary, "summary")
            progress_line += f" | {_safe_progress_summary(redacted_summary)}"
        progress_line += "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(f"{line}\n")
            if self.progress_path is not None:
                self.progress_path.parent.mkdir(parents=True, exist_ok=True)
                with self.progress_path.open("a", encoding="utf-8") as stream:
                    stream.write(progress_line)
        if self.echo:
            print(progress_line.rstrip(), file=sys.stderr, flush=True)

    def _redact(self, value: Any, key: str = "") -> Any:
        normalized_key = key.lower()
        if normalized_key not in SAFE_TOKEN_COUNT_KEYS and any(
            part in normalized_key for part in SENSITIVE_KEY_PARTS
        ):
            return "[REDACTED]"
        if isinstance(value, dict):
            return {
                str(item_key): self._redact(item, str(item_key)) for item_key, item in value.items()
            }
        if isinstance(value, (list, tuple, set, frozenset)):
            return [self._redact(item) for item in value]
        if isinstance(value, str):
            redacted = value
            for secret in self.secrets:
                redacted = redacted.replace(secret, "[REDACTED]")
            return redacted
        return value


def _safe_progress_summary(value: Any) -> str:
    """Return one bounded terminal-safe line for a trace summary."""
    text = str(value)
    without_controls = "".join(
        " " if unicodedata.category(character) in {"Cc", "Cf"} else character
        for character in text
    )
    return " ".join(without_controls.split())[:MAXIMUM_PROGRESS_SUMMARY_CHARACTERS]
