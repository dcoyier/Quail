"""Operator entrypoint. CLI reads config only; it never writes the TOML."""

from __future__ import annotations

import argparse
import sys

from quail.config import ConfigError, load_config
from quail.run import serve
from quail.run.process import process_config


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="quail", description="Quail operator CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser(
        "run",
        help="Serve MCP if already processed (never activates)",
        description=(
            "Take a deployment lease, import without activating, fail closed unless "
            "each imported version is already active and warm, then serve MCP. "
            "Never activates. Unrestricted and Clerk both use this command."
        ),
    )
    run_parser.add_argument(
        "--config",
        required=True,
        help="Absolute path to quail.toml (CLI never writes this file)",
    )

    process_parser = sub.add_parser(
        "process",
        help="Apply slim quail.toml then warm Lexical/embeddings for search",
    )
    process_parser.add_argument(
        "--config",
        required=True,
        help="Absolute path to quail.toml (CLI never writes this file)",
    )
    process_parser.add_argument(
        "--clear",
        action="store_true",
        help="Wipe search warm/vectors/FTS for active versions, then re-warm",
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
    elif args.command == "process":
        try:
            config = load_config(args.config)
            outcome = process_config(config, clear=args.clear)
            if not outcome.results:
                print("quail process: no search database; apply only")
                return
            for result in outcome.results:
                emb = "yes" if result.embedding_ready else "no"
                print(
                    f"quail process: {result.dataset_id} version={result.version_id} "
                    f"texts={result.text_count} unique={result.unique_text_count} "
                    f"embed_batches={result.embedded_batches} "
                    f"lexical=yes embedding={emb}"
                )
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
