import argparse

from ingestion.models import BuildSnapshotRequest
from ingestion.service import IngestorService


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a Boolean IAM coverage snapshot")
    parser.add_argument("--source", action="append", default=[])
    parser.add_argument("--detection", action="append", default=[])
    parser.add_argument("--non-strict", action="store_true")
    arguments = parser.parse_args()
    snapshot = IngestorService().build(
        BuildSnapshotRequest(
            enabled_detection_ids=tuple(arguments.detection),
            enabled_sources=tuple(arguments.source),
            strict=not arguments.non_strict,
        )
    )
    print(snapshot.matrix_version)
    print(
        f"{len(snapshot.permissions)} permissions, "
        f"{len(snapshot.detections)} enabled detections, "
        f"{len(snapshot.techniques)} techniques"
    )


if __name__ == "__main__":
    main()

