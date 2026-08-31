"""Controlled workspace for explicitly approved model-authored files."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from core.config import Paths
from core.errors import DataConsistencyError
from core.models import ActionArtifact

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class ActionWorkspace:
    """Write one approved artifact beneath the generated runtime directory."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or Paths().runtime / "action-agent"

    def write(
        self,
        engagement_id: str,
        candidate_id: str,
        artifact: ActionArtifact,
    ) -> ActionArtifact:
        """Write an approved artifact once and return its persisted reference."""
        if artifact.written:
            raise DataConsistencyError("action artifact was already written")
        if not all(
            _IDENTIFIER_PATTERN.fullmatch(value)
            for value in (engagement_id, candidate_id)
        ):
            raise DataConsistencyError("action artifact identifier is invalid")
        directory = (self.root / engagement_id / candidate_id).resolve()
        root = self.root.resolve()
        if not directory.is_relative_to(root):
            raise DataConsistencyError("action artifact path escapes its workspace")
        path = directory / artifact.path
        directory.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x", encoding="utf-8", newline="") as stream:
                stream.write(artifact.content)
            path.chmod(0o400)
        except FileExistsError:
            try:
                existing = path.read_text(encoding="utf-8")
            except OSError:
                raise DataConsistencyError(
                    "existing action artifact cannot be inspected"
                ) from None
            digest = "sha256:" + hashlib.sha256(existing.encode("utf-8")).hexdigest()
            if digest != artifact.digest:
                raise DataConsistencyError(
                    "action artifact path already contains different content"
                ) from None
        except OSError:
            raise DataConsistencyError("action artifact could not be written") from None
        return artifact.model_copy(
            update={"written": True, "workspace_path": str(path)}
        )
