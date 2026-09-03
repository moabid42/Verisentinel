"""Compatibility entrypoint for the unified corpus-build command."""

import sys

from runner.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["corpus", "build", *sys.argv[1:]]))
