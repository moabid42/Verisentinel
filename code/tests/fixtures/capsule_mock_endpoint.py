"""Private mock endpoint used by the local-capsule integration test."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TOKEN_PATH = Path("/tmp/expected-token")
MAXIMUM_REQUEST_BYTES = 65_536


class Handler(BaseHTTPRequestHandler):
    """Validate one typed operation and return a provider-neutral observation."""

    def do_POST(self) -> None:  # noqa: N802
        expected = f"Bearer {TOKEN_PATH.read_text(encoding='utf-8').strip()}"
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400)
            return
        if (
            self.path != "/execute"
            or self.headers.get("Authorization") != expected
            or not 0 < length <= MAXIMUM_REQUEST_BYTES
        ):
            self.send_error(403)
            return
        try:
            document = json.loads(self.rfile.read(length))
            _validate_request(document)
        except (TypeError, ValueError, json.JSONDecodeError):
            self.send_error(400)
            return
        time.sleep(0.2)
        observation = {
            "action_id": document["action_id"],
            "api_response_summary": "private mock accepted typed operation",
            "discovered_resources": [],
            "engagement_id": document["engagement_id"],
            "execution_id": f"capsule-{document['approval_id']}",
            "gained_capabilities": document["expected_capabilities"],
            "gained_permissions": [],
            "identity": document["identity"],
            "observed_permission_footprint": document[
                "observed_permission_footprint"
            ],
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


def _validate_request(document: object) -> None:
    keys = {
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
    if not isinstance(document, dict) or set(document) != keys:
        raise ValueError("unexpected request shape")
    if document["operation"] != "catalog.technique":
        raise ValueError("unexpected operation")
    if document["parameters"] != {}:
        raise ValueError("unexpected parameters")
    if any("command" in key for key in document):
        raise ValueError("command input is prohibited")


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
