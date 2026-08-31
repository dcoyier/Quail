"""Host preset: load the analysis API into a session.

The host runs this automatically. The agent does not execute it. Nothing
prints. The files in SURFACE are the API the agent is shown; this block
only reads them into one namespace.

Live retrieve, count, create_field, tag, untag, and print are bound by the
host after this returns. They are not loaded here.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

API_DIR = Path(__file__).resolve().parent / "api"

# Load order: later files may use names from earlier files.
# unit.py defines Unit, entries, fields. group.py defines GroupExpr, G0, G1.
SURFACE: tuple[str, ...] = (
    "errors.py",
    "field.py",
    "operations.py",
    "expression.py",
    "predicate.py",
    "unit.py",
    "entry.py",
    "group.py",
    "ranking.py",
    "re.py",
)

HOST_BOUND: tuple[str, ...] = (
    "retrieve",
    "count",
    "create_field",
    "tag",
    "untag",
    "print",
)

EXPLANATION = (
    "You write analysis cells against a library that is already loaded. "
    "Only print returns. retrieve and count read the grid; tag writes the "
    "session overlay. Source data never changes. The files in api/ are the API."
)


def _reject_print(*args: Any, **kwargs: Any) -> None:
    del args, kwargs
    raise RuntimeError("the setup preset cannot print")


def run(
    api_dir: Path | None = None,
    surface: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Read SURFACE files and exec them into one namespace. Print nothing."""

    root = (API_DIR if api_dir is None else Path(api_dir)).resolve()
    names = tuple(SURFACE if surface is None else surface)
    namespace: dict[str, Any] = {"__name__": "quail_api", "print": _reject_print}
    try:
        for filename in names:
            path = _surface_path(root, filename)
            source = path.read_text(encoding="utf-8")
            exec(compile(source, str(path), "exec"), namespace)  # noqa: S102
    finally:
        if namespace.get("print") is _reject_print:
            del namespace["print"]
    return namespace


def _surface_path(root: Path, filename: str) -> Path:
    if Path(filename).name != filename:
        raise ValueError(f"setup surface file must be a bare name, not {filename!r}")
    path = (root / filename).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"setup surface file escapes the API directory: {filename!r}")
    if not path.is_file():
        raise FileNotFoundError(f"setup surface file is missing: {path}")
    return path
