"""Fixed command for uploading one approved action envelope to GCS."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

MAXIMUM_INPUT_BYTES = 65_536
MAXIMUM_OUTPUT_BYTES = 32_768
_BUCKET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$")
_OBJECT_PATTERN = re.compile(
    r"^actions/approval_[0-9a-f]{32}\.json$"
)


class UploadCommandError(RuntimeError):
    """A fixed upload command input or request failed."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def upload(
    *,
    bucket: str,
    object_name: str,
    encoded_input: bytes,
    opener: Callable[..., Any] | None = None,
) -> bytes:
    """Validate stdin, upload its envelope, and return exact bounded API output."""
    if _BUCKET_PATTERN.fullmatch(bucket) is None:
        raise UploadCommandError("invalid_target")
    if _OBJECT_PATTERN.fullmatch(object_name) is None:
        raise UploadCommandError("invalid_target")
    if not encoded_input or len(encoded_input) > MAXIMUM_INPUT_BYTES:
        raise UploadCommandError("invalid_input")
    try:
        command_input = json.loads(encoded_input)
        authorization = command_input["authorization"]
        document = command_input["document"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
        raise UploadCommandError("invalid_input") from None
    if (
        not isinstance(command_input, dict)
        or set(command_input) != {"authorization", "document"}
        or not isinstance(authorization, str)
        or not authorization.startswith("Bearer ")
        or not isinstance(document, dict)
    ):
        raise UploadCommandError("invalid_input")
    expected_name = f"actions/{document.get('approval_id')}.json"
    if object_name != expected_name:
        raise UploadCommandError("invalid_target")
    encoded_document = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    query = urlencode({"uploadType": "media", "name": object_name})
    request = Request(
        f"https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o?{query}",
        data=encoded_document,
        headers={
            "Authorization": authorization,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with (opener or urlopen)(request, timeout=20.0) as response:
            output = response.read(MAXIMUM_OUTPUT_BYTES + 1)
    except (HTTPError, URLError, OSError, TimeoutError):
        raise UploadCommandError("request_failed") from None
    if len(output) > MAXIMUM_OUTPUT_BYTES:
        raise UploadCommandError("output_exceeded")
    try:
        output.decode("utf-8")
    except UnicodeDecodeError:
        raise UploadCommandError("invalid_output") from None
    return output


def main(arguments: list[str] | None = None) -> int:
    """Run the fixed upload command with credential material read from stdin."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--object", required=True, dest="object_name")
    try:
        parsed = parser.parse_args(arguments)
        encoded_input = sys.stdin.buffer.read(MAXIMUM_INPUT_BYTES + 1)
        output = upload(
            bucket=parsed.bucket,
            object_name=parsed.object_name,
            encoded_input=encoded_input,
        )
        sys.stdout.buffer.write(output)
        if output and not output.endswith(b"\n"):
            sys.stdout.buffer.write(b"\n")
        return 0
    except (SystemExit, UploadCommandError) as error:
        code = error.code if isinstance(error, UploadCommandError) else "invalid_arguments"
        print(f"gcs_upload_error:{code}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
