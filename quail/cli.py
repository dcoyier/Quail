"""Operator entrypoint. CLI reads config only; it never writes the TOML."""

from __future__ import annotations

import argparse
import sys

from quail.config import ConfigError, load_config
from quail.run import serve


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="quail", description="Quail operator CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser(
        "run",
        help="Apply slim quail.toml then serve unrestricted loopback MCP",
    )
    run_parser.add_argument(
        "--config",
        required=True,
        help="Absolute path to quail.toml (CLI never writes this file)",
    )

    args = parser.parse_args(argv)
    if args.command == "run":
        try:
            config = load_config(args.config)
            serve(config)
        except ConfigError as error:
            print(f"quail: {error}", file=sys.stderr)
            raise SystemExit(2) from error
        except Exception as error:
            print(f"quail: {error}", file=sys.stderr)
            raise SystemExit(1) from error
    else:
        parser.error(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
