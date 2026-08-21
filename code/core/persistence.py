from __future__ import annotations

from pathlib import Path
from threading import RLock

from pydantic import BaseModel

from core.errors import NotFoundError


class JsonModelStore[ModelT: BaseModel]:
    """Small durable store for immutable artifacts and development deployments."""

    def __init__(self, directory: Path, model: type[ModelT]) -> None:
        self.directory = directory
        self.model = model
        self._lock = RLock()

    def put(self, key: str, value: ModelT) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{key}.json"
        temporary = path.with_suffix(".json.tmp")
        with self._lock:
            temporary.write_text(value.model_dump_json(indent=2), encoding="utf-8")
            temporary.replace(path)
        return path

    def get(self, key: str) -> ModelT:
        path = self.directory / f"{key}.json"
        if not path.exists():
            raise NotFoundError(f"{self.model.__name__} {key!r} does not exist")
        return self.model.model_validate_json(path.read_text(encoding="utf-8"))

    def list_keys(self) -> tuple[str, ...]:
        if not self.directory.exists():
            return ()
        return tuple(sorted(path.stem for path in self.directory.glob("*.json")))
