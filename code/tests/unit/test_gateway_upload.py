"""Tests for the fixed GCS upload command executed by the gateway."""

import json
from urllib.parse import parse_qs, urlparse

import pytest

from execution.gateway.gcs_upload import (
    MAXIMUM_OUTPUT_BYTES,
    UploadCommandError,
    upload,
)

APPROVAL_ID = "approval_" + "1" * 32
OBJECT_NAME = f"actions/{APPROVAL_ID}.json"


class Response:
    """Return fixed raw bytes through the urllib response protocol."""

    def __init__(self, output: bytes) -> None:
        self.output = output

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *arguments: object) -> None:
        del arguments

    def read(self, limit: int) -> bytes:
        return self.output[:limit]


def encoded_input(*, approval_id: str = APPROVAL_ID) -> bytes:
    """Build the private stdin document consumed by the command."""
    return json.dumps(
        {
            "authorization": "Bearer synthetic-secret",
            "document": {
                "action_id": "technique:test",
                "approval_id": approval_id,
            },
        }
    ).encode()


def test_upload_returns_exact_api_stdout_and_sends_token_only_as_header() -> None:
    raw_output = b'{"bucket":"scenario-target","name":"actions/object.json"}'
    requests = []

    def opener(request, *, timeout: float) -> Response:
        assert timeout == 20.0
        requests.append(request)
        return Response(raw_output)

    output = upload(
        bucket="scenario-target",
        object_name=OBJECT_NAME,
        encoded_input=encoded_input(),
        opener=opener,
    )

    assert output == raw_output
    request = requests[0]
    assert request.get_header("Authorization") == "Bearer synthetic-secret"
    assert b"synthetic-secret" not in request.data
    query = parse_qs(urlparse(request.full_url).query)
    assert query == {"uploadType": ["media"], "name": [OBJECT_NAME]}


def test_upload_rejects_object_that_does_not_match_approval() -> None:
    with pytest.raises(UploadCommandError, match="invalid_target"):
        upload(
            bucket="scenario-target",
            object_name="actions/approval_" + "2" * 32 + ".json",
            encoded_input=encoded_input(),
            opener=lambda *_args, **_kwargs: pytest.fail("request must not run"),
        )


def test_upload_rejects_unbounded_api_output() -> None:
    def opener(_request, *, timeout: float) -> Response:
        del timeout
        return Response(b"x" * (MAXIMUM_OUTPUT_BYTES + 1))

    with pytest.raises(UploadCommandError, match="output_exceeded"):
        upload(
            bucket="scenario-target",
            object_name=OBJECT_NAME,
            encoded_input=encoded_input(),
            opener=opener,
        )
