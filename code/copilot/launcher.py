"""Sanitize the environment and launch DeepSeek Harness inside bubblewrap."""

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
    """Replace this process with the isolated harness runtime."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--bubblewrap", required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--cordis", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--sessions", type=Path, required=True)
    args = parser.parse_args()
    environment = {key: os.environ[key] for key in _PASSTHROUGH_ENVIRONMENT if key in os.environ}
    environment.update(
        {
            "HOME": "/home/harness",
            "LANG": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
        }
    )
    command = _bubblewrap_command(args)
    os.execvpe(args.bubblewrap, command, environment)


def _bubblewrap_command(args: argparse.Namespace) -> tuple[str, ...]:
    command = [
        args.bubblewrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--share-net",
        "--ro-bind",
        "/usr",
        "/usr",
        "--symlink",
        "usr/bin",
        "/bin",
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib64",
        "/lib64",
        "--dir",
        "/etc",
    ]
    for source in ("/etc/hosts", "/etc/nsswitch.conf", "/etc/resolv.conf", "/etc/ssl"):
        if Path(source).exists():
            command.extend(("--ro-bind", source, source))
    command.extend(
        (
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--dir",
            "/home",
            "--dir",
            "/home/harness",
            "--dir",
            "/runtime",
            "--ro-bind",
            str(args.runtime),
            "/runtime/dsh",
            "--ro-bind",
            str(args.cordis),
            "/runtime/cordis.yml",
            "--bind",
            str(args.workspace),
            "/workspace",
            "--bind",
            str(args.sessions),
            "/sessions",
            "--chdir",
            "/workspace",
            "/runtime/dsh",
        )
    )
    return tuple(command)


if __name__ == "__main__":
    main()
