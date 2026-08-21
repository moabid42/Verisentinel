from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def repository_root() -> Path:
    configured = os.getenv("IAM_PLANNER_REPOSITORY_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class Paths:
    repository: Path = repository_root()

    @property
    def code(self) -> Path:
        return self.repository / "code"

    @property
    def iam_dataset(self) -> Path:
        return Path(
            os.getenv(
                "IAM_DATASET_PATH",
                self.repository / "data" / "iam-dataset" / "gcp",
            )
        ).resolve()

    @property
    def iamouflage_data(self) -> Path:
        return Path(
            os.getenv(
                "IAMOUFLAGE_DATA_PATH",
                self.code / "IAMouflage" / "code" / "data",
            )
        ).resolve()

    @property
    def artifacts(self) -> Path:
        return Path(os.getenv("IAM_PLANNER_ARTIFACTS", self.code / "artifacts")).resolve()

    @property
    def runtime(self) -> Path:
        return Path(os.getenv("IAM_PLANNER_RUNTIME", self.code / "runtime")).resolve()


@dataclass(frozen=True, slots=True)
class ServiceURLs:
    ingestor: str = os.getenv("INGESTOR_URL", "http://ingestor:8001")
    environment: str = os.getenv("ENVIRONMENT_URL", "http://environment:8002")
    validator: str = os.getenv("VALIDATOR_URL", "http://validator:8003")
    proposer: str = os.getenv("PROPOSER_URL", "http://proposer:8004")
    green_agent: str = os.getenv("GREEN_AGENT_URL", "http://green-agent:8005")
    launchpad: str = os.getenv("LAUNCHPAD_URL", "http://launchpad:8006")
    execution: str = os.getenv("EXECUTION_URL", "http://execution:8007")

