"""Launch the DeepSeek SDK runtime inside a least-privilege container."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

_PASSTHROUGH_ENVIRONMENT = (
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "DSH_CORDIS_CONFIG",
    "DSH_CWD",
    "DSH_SESSION_ROOT",
)


def main() -> None:
    """Replace this process with an attached isolated runtime container."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--docker", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--cordis", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--sessions", type=Path, required=True)
    args = parser.parse_args()
    environment = {key: os.environ[key] for key in _PASSTHROUGH_ENVIRONMENT if key in os.environ}
    environment["PATH"] = "/usr/bin:/bin"
    os.execvpe(args.docker, _command(args), environment)


def _command(args: argparse.Namespace) -> tuple[str, ...]:
    runtime_root = args.runtime.parent
    return (
        args.docker,
        "run",
        "--rm",
        "--interactive",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges=true",
        "--pids-limit",
        "64",
        "--memory",
        "512m",
        "--memory-swap",
        "512m",
        "--cpus",
        "1",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=64m,mode=1777",
        "--mount",
        f"type=bind,src={runtime_root},dst=/runtime,readonly",
        "--mount",
        f"type=bind,src={args.workspace},dst=/workspace",
        "--mount",
        f"type=bind,src={args.sessions},dst=/sessions",
        "--mount",
        "type=bind,src=/etc/ssl/certs,dst=/etc/ssl/certs,readonly",
        "--env",
        "DEEPSEEK_API_KEY",
        "--env",
        "DEEPSEEK_BASE_URL",
        "--env",
        "DSH_CORDIS_CONFIG",
        "--env",
        "DSH_CWD",
        "--env",
        "DSH_SESSION_ROOT",
        "--entrypoint",
        f"/runtime/{args.runtime.name}",
        args.image,
    )


if __name__ == "__main__":
    main()
