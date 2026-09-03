"""Tests for explicitly approved model-authored file writes."""

import hashlib
from pathlib import Path

import pytest

from action_agent.workspace import ActionWorkspace
from core.errors import DataConsistencyError
from core.models import ActionArtifact


def artifact(content: str = "print('approved')\n") -> ActionArtifact:
    return ActionArtifact(
        content=content,
        digest="sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest(),
        source_model="fixture-model",
        rationale="Exercise the approved action.",
    )


def test_workspace_writes_exact_approved_content_once(tmp_path: Path) -> None:
    workspace = ActionWorkspace(tmp_path / "actions")

    written = workspace.write("engagement", "candidate", artifact())

    path = Path(written.workspace_path)
    assert written.written
    assert path.read_text(encoding="utf-8") == artifact().content
    assert path.stat().st_mode & 0o777 == 0o400


def test_workspace_refuses_to_replace_different_content(tmp_path: Path) -> None:
    workspace = ActionWorkspace(tmp_path / "actions")
    first = workspace.write("engagement", "candidate", artifact())

    with pytest.raises(DataConsistencyError, match="different content"):
        workspace.write("engagement", "candidate", artifact("print('changed')\n"))

    assert Path(first.workspace_path).read_text(encoding="utf-8") == artifact().content
