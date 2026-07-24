"""Sandbox method policy and assign/del tracking."""

from __future__ import annotations

import pytest

from quail.analysis.errors import QuailSyntaxError
from quail.analysis.worker.sandbox import validate_quail_code


def test_sandbox_rejects_container_mutation_and_unlisted_methods() -> None:
    with pytest.raises(QuailSyntaxError, match="append"):
        validate_quail_code("xs = []\nxs.append(1)\n")
    with pytest.raises(QuailSyntaxError, match="encode"):
        validate_quail_code("x = 'a'.encode()\n")
    with pytest.raises(QuailSyntaxError, match="re.compile"):
        validate_quail_code("re.compile('x')\n")


def test_sandbox_allows_approved_string_and_quail_methods() -> None:
    program = validate_quail_code(
        "text = 'Garden'.lower()\n"
        "ok = text.startswith('g')\n"
        "parts = text.split()\n"
        "escaped = re.escape('a+b')\n"
        "group = G0.where(Expression(Field('body'), Length()) > 0)\n"
    )
    assert "text" in program.assigned_names
    assert "group" in program.assigned_names


def test_sandbox_tracks_assign_and_del() -> None:
    program = validate_quail_code("x = 1\ndel x\ny = 2\n")
    assert program.assigned_names == frozenset({"x", "y"})
    assert program.deleted_names == frozenset({"x"})
