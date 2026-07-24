"""Bounded RE2 regex programs for analysis operations."""

from __future__ import annotations

import re as python_re
from dataclasses import dataclass
from typing import Any

import re2

from quail.analysis.errors import QuailSyntaxError
from quail.analysis.re_helper import require_regex_text, validate_regex_flags


@dataclass(frozen=True, slots=True)
class RegexProgram:
    """Compiled RE2 pattern with search / find_all / sub_literal."""

    regex: Any

    def search(self, text: str) -> str | None:
        matched = self.regex.search(text)
        if matched is None:
            return None
        return str(matched.group(0))

    def find_all(self, text: str) -> list[str]:
        return [str(matched.group(0)) for matched in self.regex.finditer(text)]

    def sub_literal(self, text: str, replacement: str) -> str:
        return str(self.regex.sub(lambda _matched: replacement, text))


def compile_regex(pattern: str, flags: int = 0) -> RegexProgram:
    """Compile a Quail regex with RE2 and map failures to QuailSyntaxError."""

    require_regex_text(pattern, "Regex pattern")
    validate_regex_flags(flags)
    prefixes = ""
    if flags & int(python_re.I):
        prefixes += "i"
    if flags & int(python_re.M):
        prefixes += "m"
    if flags & int(python_re.S):
        prefixes += "s"
    source = f"(?{prefixes}){pattern}" if prefixes else pattern
    options = re2.Options()
    options.log_errors = False
    options.max_mem = 4 * 1024 * 1024
    try:
        return RegexProgram(re2.compile(source, options))
    except re2.error as error:
        raise QuailSyntaxError(public_regex_error(pattern, native_message=str(error))) from error


def public_regex_error(pattern: str, *, native_message: str = "") -> str:
    prefix = "Invalid regex pattern: "
    if python_re.search(r"(?<!\\)(?:\\\\)*\(\?(?:[=!]|<[=!])", pattern) is not None:
        return prefix + "lookaround is not supported"
    if (
        python_re.search(
            r"(?<!\\)(?:\\\\)*\\(?:[1-9]|g<|k<)|(?<!\\)(?:\\\\)*\(\?P=",
            pattern,
        )
        is not None
    ):
        return prefix + "backreferences are not supported"
    if native_message:
        return prefix + native_message
    return prefix + "this syntax is not supported by Quail regexes"
