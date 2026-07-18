"""The ``remote-jobs`` command line interface (SPEC section 14)."""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from .._version import __version__
from . import publish


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog="remote-jobs",
        description="Tooling for Nautobot remote jobs (publish job definitions, etc.).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    publish.add_parser(subparsers)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Console script entry point; returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except publish.PublishError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
