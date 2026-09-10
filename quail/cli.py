"""Argument parsing and presentation for the local Quail commands.

Project operations live in service; live execution lives behind the local
adapter. Importing this module never loads NumPy or a process-lifecycle graph.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Literal

from quail import service
from quail.contracts import ErrorInfo, JSONObject, JSONValue, QuailError, canonical_json
from quail.project import Project, discover


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quail", description="An environment for agentic qualitative analysis"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Create an empty study")
    init.add_argument("directory", nargs="?", type=Path, default=Path("."))
    ingest = commands.add_parser("import", help="Register a source CSV and build its index")
    ingest.add_argument("csv", type=Path)
    ingest.add_argument("--name")
    ingest.add_argument("--id", dest="id_column")
    ingest.add_argument("--embed")
    ingest.add_argument("--embed-revision")
    commands.add_parser("info", help="Inspect datasets, history, and local runtime state")
    execute = commands.add_parser("exec", help="Submit one cell to a persistent session")
    execute.add_argument("session")
    execute.add_argument("file", nargs="?", type=Path)
    mode = execute.add_mutually_exclusive_group()
    mode.add_argument("-c", dest="code")
    mode.add_argument("--reset", action="store_true")
    mode.add_argument("--close", action="store_true")
    execute.add_argument("--dataset")
    execute.add_argument("--fork-from")
    commands.add_parser("sessions", help="List analysis sessions")
    fork = commands.add_parser("fork", help="Copy a closed session's history")
    fork.add_argument("source")
    fork.add_argument("destination")
    fields = commands.add_parser("fields", help="List source and optional session tag fields")
    fields.add_argument("dataset")
    fields.add_argument("--session")
    export = commands.add_parser("export", help="Export source fields and committed tags")
    export.add_argument("session")
    export.add_argument("--out", type=Path)
    warm = commands.add_parser("warm", help="Cache source embeddings and optionally share a shard")
    warm.add_argument("dataset")
    warm.add_argument("--field")
    warm.add_argument("--shard")
    for command in ("info", "exec", "sessions", "fields", "export", "warm"):
        commands.choices[command].add_argument("--json", action="store_true")
    return parser


def _progress(message: str) -> None:
    print(message, file=sys.stderr)


def _inspection(project: Project) -> JSONObject:
    from quail import local

    # Runtime inspection does not depend on a successful source rebuild. Keep
    # enough status to identify what needs closing when the CSV has changed.
    statuses = {name: local.runtime(project, name) for name in project.session_names()}
    result = service.info(project)
    sessions = result["sessions"]
    assert isinstance(sessions, list)
    for session in sessions:
        assert isinstance(session, dict)
        session["runtime"] = statuses.get(str(session["name"]), {"state": "stopped"})
    return result


def _operation(args: argparse.Namespace) -> JSONValue:
    if args.command == "init":
        return {"path": str(service.initialize(args.directory).root)}
    # Read a file before starting or contacting any host. It is exactly one cell.
    if args.command == "exec" and args.file is not None:
        args.code = args.file.read_text(encoding="utf-8")
    project = discover()
    match args.command:
        case "import":
            return service.import_csv(
                project,
                args.csv,
                name=args.name,
                id_column=args.id_column,
                embed=args.embed,
                revision=args.embed_revision,
            )
        case "info":
            return _inspection(project)
        case "sessions":
            return _inspection(project)["sessions"]
        case "fields":
            return list(service.fields(project, args.dataset, args.session))
        case "fork":
            result = service.fork(project, args.source, args.destination)
            return {
                "session": result.name,
                "dataset": result.dataset,
                "forked_from": result.forked_from,
            }
        case "export":
            return service.export(project, args.session, args.out)
        case "warm":
            return service.warm(
                project, args.dataset, field=args.field, shard=args.shard, progress=_progress
            )
        case "exec":
            from quail import local

            mode: Literal["exec", "reset", "close"] = (
                "reset" if args.reset else "close" if args.close else "exec"
            )
            return local.call(
                project,
                args.session,
                mode,
                code=args.code,
                dataset=args.dataset,
                fork_from=args.fork_from,
                progress=_progress,
            )
    raise AssertionError(f"Unhandled command: {args.command}")


def _present(args: argparse.Namespace, result: JSONValue) -> int:
    if isinstance(result, dict):
        warnings = result.get("warnings", [])
        if isinstance(warnings, list):
            for warning in warnings:
                _progress(str(warning))
    if getattr(args, "json", False):
        print(canonical_json(result))
    elif args.command == "exec":
        assert isinstance(result, dict)
        if args.reset:
            print(f"Reset kernel for {args.session!r}; committed tags retained.")
        elif args.close:
            print(f"Closed {args.session!r}.")
        else:
            output = result.get("output", "")
            assert isinstance(output, str)
            print(output, end="")
    elif args.command == "init":
        assert isinstance(result, dict)
        print(f"Created study at {result['path']}")
    elif args.command == "import":
        assert isinstance(result, dict)
        print(f"Imported {result['name']!r}: {result['rows']} entries")
        if result["generated_ids"]:
            _progress(
                "Generated IDs belong to this source version; "
                "preserve them explicitly before editing the CSV."
            )
    elif args.command == "export":
        assert isinstance(result, dict)
        print(
            f"Exported {result['rows']} entries to {result['path']} "
            f"({result['orphans']} orphan tags)"
        )
    elif args.command == "fork":
        assert isinstance(result, dict)
        print(f"Forked {result['forked_from']!r} to {result['session']!r}")
    elif args.command == "warm":
        assert isinstance(result, dict)
        print(f"Warmed {result['selected']} values: {result['reused']} reused, {result['new']} new")
        if result["pack"] is not None:
            print(f"Published {result['pack']} ({result['bytes']} bytes)")
    else:
        # Orientation is structured data even in its readable form; execution
        # alone has notebook stdout. No secondary result schema is needed.
        import json

        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return int(isinstance(result, dict) and result.get("error") is not None)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "exec":
        if sum((args.code is not None, args.file is not None, args.reset, args.close)) != 1:
            parser.error("exec requires exactly one of -c CODE, FILE.py, --reset, or --close")
        if (args.reset or args.close) and (args.dataset is not None or args.fork_from is not None):
            parser.error("--dataset and --fork-from apply only to code submission")
    try:
        return _present(args, _operation(args))
    except (QuailError, OSError, UnicodeError, sqlite3.Error) as error:
        information = ErrorInfo.from_exception(error)
        if getattr(args, "json", False):
            print(canonical_json({"error": information.to_record()}))
        _progress(f"{information.type}: {information.message}")
        if information.hint:
            _progress("Hint: " + information.hint)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
