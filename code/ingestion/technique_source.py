"""Resolve a technique_id to the source text it was extracted from.

The permission-indexed ``MatrixSnapshot`` deliberately carries no provenance, so this reads the
raw IAMouflage technique export (``techniques.json`` — the same file the ingestor consumes) plus
the vendored corpora, and returns the section a technique was lifted from. It mirrors IAMouflage's
own ``get_technique_source``: a hacktricks technique is the block from its heading down to the next
heading of equal-or-higher level; stratus/custom techniques return their whole definition file.

Everything degrades gracefully — a missing export or an un-checked-out corpus yields ``None`` — so
the caller (the plan writer) can fall back to scenario metadata instead of failing the run.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

# Only ##/###/#### delimit a technique section (mirrors IAMouflage's parser and mcp_server).
_HEADING_RE = re.compile(r"^(#{2,4})\s+")

# Corpus layout inside the IAMouflage repo, mirroring IAMouflage/code/core/corpus.py. Each
# technique's ``rel_path`` is stored relative to one of these roots, keyed by its ``source``.
_HACKTRICKS_SUBDIR = (
    "data", "techniques", "hacktricks-cloud", "src", "pentesting-cloud", "gcp-security",
)
_STRATUS_SUBDIR = (
    "data", "techniques", "stratus-red-team", "v2", "internal", "attacktechniques", "gcp",
)


@dataclass(frozen=True, slots=True)
class TechniqueSource:
    technique_id: str
    source: str
    path: Path
    markdown: str


class TechniqueSourceResolver:
    """Look up the raw source section for a technique, given the IAMouflage data directory."""

    def __init__(self, iamouflage_data: Path) -> None:
        # ``iamouflage_data`` is .../IAMouflage/code/data (holds techniques.json); the vendored
        # corpora live under .../IAMouflage/data/techniques, i.e. two levels up.
        self._data = iamouflage_data
        self._repo = iamouflage_data.parents[1]
        self._records: dict[str, dict] | None = None

    def _load(self) -> dict[str, dict]:
        if self._records is None:
            try:
                raw = json.loads((self._data / "techniques.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = []
            records = raw if isinstance(raw, list) else []
            self._records = {
                str(record["id"]): record
                for record in records
                if isinstance(record, dict) and record.get("id")
            }
        return self._records

    def resolve(self, technique_id: str) -> TechniqueSource | None:
        record = self._load().get(technique_id)
        if record is None:
            return None
        rel = record.get("rel_path")
        source = str(record.get("source") or "hacktricks")
        if not rel:
            return None

        # Custom techniques carry their whole body in a YAML file under IAMouflage/code; stratus
        # techniques are self-contained Go packages. Neither has a heading section to slice.
        if record.get("extraction") == "custom" or source == "custom":
            return self._whole_file(technique_id, source, self._repo / "code" / rel)
        if record.get("extraction") == "stratus" or source == "stratus":
            path = self._repo.joinpath(*_STRATUS_SUBDIR) / rel
            return self._whole_file(technique_id, source, path)

        # hacktricks: slice from the technique's heading to the next equal-or-higher heading.
        path = self._repo.joinpath(*_HACKTRICKS_SUBDIR) / rel
        if not path.is_file():
            return None
        start = record.get("line")
        if not start:
            return None
        lines = path.read_text(encoding="utf-8").splitlines()
        level = record.get("heading_level")
        if level is None:
            end = start  # inline mention: the technique is a single line
        else:
            end = len(lines)
            for index in range(start, len(lines)):
                match = _HEADING_RE.match(lines[index])
                if match and len(match.group(1)) <= level:
                    end = index
                    break
        markdown = "\n".join(lines[start - 1 : end]).strip()
        return TechniqueSource(technique_id, source, path, markdown)

    @staticmethod
    def _whole_file(technique_id: str, source: str, path: Path) -> TechniqueSource | None:
        if not path.is_file():
            return None
        return TechniqueSource(
            technique_id, source, path, path.read_text(encoding="utf-8").strip()
        )
