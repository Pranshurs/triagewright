"""Command-line entry point."""

from __future__ import annotations

import argparse

from fdeops import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fdeops")
    parser.add_argument("--version", action="version", version=__version__)
    parser.parse_args(argv)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
