"""Tests for structured diagnostic tracing."""

from pathlib import Path

from core.tracing import DebugTrace


def test_progress_summary_is_redacted_bounded_and_terminal_safe(
    tmp_path: Path,
) -> None:
    progress_path = tmp_path / "progress.log"
    trace = DebugTrace(
        tmp_path / "trace.jsonl",
        "run",
        secrets=("sensitive-value",),
        echo=False,
        progress_path=progress_path,
    )

    trace.emit(
        "model",
        "completed",
        summary=(
            "ranked\n\x1b[31msensitive-value\x1b[0m " + "x" * 1_000
        ),
    )

    progress = progress_path.read_text(encoding="utf-8")
    assert len(progress.splitlines()) == 1
    assert "\x1b" not in progress
    assert "sensitive-value" not in progress
    assert "ranked [31m[REDACTED] [0m" in progress
    assert len(progress.partition(" | ")[2].rstrip("\n")) == 500
