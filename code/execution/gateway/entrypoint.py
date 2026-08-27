"""Fixed HTTP gateway for approved action delivery to one GCS bucket."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

CONFIGURATION_PATH = Path("/run/verisentinel/connection.json")
MAXIMUM_REQUEST_BYTES = 65_536
MAXIMUM_RESPONSE_BYTES = 65_536
_REQUEST_KEYS = {
    "action_id",
    "approval_id",
    "engagement_id",
    "expected_capabilities",
    "identity",
    "observed_permission_footprint",
    "operation",
    "parameters",
    "target",
}
_PROJECT_PATTERN = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_BUCKET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$")


class GatewayError(RuntimeError):
    """A gateway request is invalid or cannot reach its fixed target."""


def load_configuration(path: Path = CONFIGURATION_PATH) -> dict[str, str]:
    """Load the exact infrastructure path and expected scenario principal."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise GatewayError("configuration unavailable") from None
    if not isinstance(document, dict) or set(document) != {
        "infrastructure_path",
        "principal",
    }:
        raise GatewayError("configuration invalid")
    if not all(isinstance(value, str) and value for value in document.values()):
        raise GatewayError("configuration invalid")
    _bucket(document["infrastructure_path"])
    return document


class Handler(BaseHTTPRequestHandler):
    """Validate one capsule request and deliver it to the configured bucket."""

    configuration: dict[str, str]

    def do_POST(self) -> None:  # noqa: N802
        try:
            document, authorization = self._request()
            if document["identity"] != self.configuration["principal"]:
                raise GatewayError("principal mismatch")
            self._upload(document, authorization)
            self._respond(document)
        except GatewayError:
            self.send_error(403)

    def _request(self) -> tuple[dict[str, Any], str]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise GatewayError("request invalid") from None
        authorization = self.headers.get("Authorization", "")
        if (
            self.path != "/execute"
            or not authorization.startswith("Bearer ")
            or not 0 < length <= MAXIMUM_REQUEST_BYTES
        ):
            raise GatewayError("request invalid")
        try:
            document = json.loads(self.rfile.read(length))
        except (UnicodeError, json.JSONDecodeError):
            raise GatewayError("request invalid") from None
        if not isinstance(document, dict) or set(document) != _REQUEST_KEYS:
            raise GatewayError("request invalid")
        if document["operation"] != "catalog.technique":
            raise GatewayError("operation invalid")
        if document["parameters"] != {}:
            raise GatewayError("parameters invalid")
        return document, authorization

    def _upload(self, document: dict[str, Any], authorization: str) -> None:
        bucket = _bucket(self.configuration["infrastructure_path"])
        name = f"actions/{document['approval_id']}.json"
        encoded = json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        query = urlencode({"uploadType": "media", "name": name})
        request = Request(
            f"https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o?{query}",
            data=encoded,
            headers={
                "Authorization": authorization,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=20.0) as response:
                response.read(MAXIMUM_RESPONSE_BYTES + 1)
        except (HTTPError, URLError, OSError, TimeoutError):
            raise GatewayError("delivery failed") from None

    def _respond(self, document: dict[str, Any]) -> None:
        observation = {
            "action_id": document["action_id"],
            "api_response_summary": ("approved action delivered to scenario infrastructure"),
            "discovered_resources": [self.configuration["infrastructure_path"]],
            "engagement_id": document["engagement_id"],
            "execution_id": f"gcp-{document['approval_id']}",
            "gained_capabilities": [],
            "gained_permissions": [],
            "identity": document["identity"],
            "observed_permission_footprint": document["observed_permission_footprint"],
            "revoked_permissions": [],
            "success": True,
            "target": document["target"],
            "timestamp": datetime.now(UTC).isoformat(),
        }
        encoded = json.dumps(
            observation,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *arguments: object) -> None:
        del format, arguments


def _bucket(path: str) -> str:
    parts = path.split("/")
    if (
        len(parts) != 4
        or parts[0] != "projects"
        or parts[2] != "buckets"
        or _PROJECT_PATTERN.fullmatch(parts[1]) is None
        or _BUCKET_PATTERN.fullmatch(parts[3]) is None
    ):
        raise GatewayError("configuration invalid")
    return parts[3]


if __name__ == "__main__":
    Handler.configuration = load_configuration()
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
