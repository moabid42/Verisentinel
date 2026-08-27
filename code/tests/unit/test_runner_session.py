"""Tests for durable interactive terminal session records."""

from pathlib import Path

import pytest

from runner.session import ShellSessionRepository, ShellSessionStatus


def test_session_repository_records_only_sanitized_operations(tmp_path: Path) -> None:
    repository = ShellSessionRepository(tmp_path)
    created = repository.create()

    updated = repository.record(
        created,
        operation="auth inspect",
        exit_code=0,
    )
    closed = repository.close(updated)
    loaded = repository.get(created.session_id)

    assert loaded == closed
    assert loaded.status == ShellSessionStatus.CLOSED
    assert loaded.command_count == 1
    assert loaded.events[0].operation == "auth inspect"
    assert "credential" not in loaded.model_dump_json()


def test_session_repository_lists_most_recent_first(tmp_path: Path) -> None:
    repository = ShellSessionRepository(tmp_path)
    first = repository.create()
    second = repository.create()
    repository.record(first, operation="corpus status", exit_code=0)

    sessions = repository.list()

    assert sessions[0].session_id == first.session_id
    assert sessions[1].session_id == second.session_id


def test_session_repository_resumes_closed_session(tmp_path: Path) -> None:
    repository = ShellSessionRepository(tmp_path)
    closed = repository.close(repository.create())

    resumed = repository.resume(closed.session_id)

    assert resumed.status == ShellSessionStatus.ACTIVE


@pytest.mark.parametrize(
    "session_id",
    ("../current", "session_missing", "session_" + "g" * 32),
)
def test_session_repository_rejects_invalid_identifier(
    tmp_path: Path,
    session_id: str,
) -> None:
    repository = ShellSessionRepository(tmp_path)

    with pytest.raises(ValueError, match="invalid format"):
        repository.get(session_id)
