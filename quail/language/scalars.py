"""Stateless scalar operations at typed SQLite boundaries.

These functions operate on one value, never query a database or call a verb.
JSON is decoded only when its compiled encoding says it is JSON; a source
string such as 'true' or '[1]' remains a string.
"""

from __future__ import annotations

import hashlib
import math
import re
import sqlite3
from collections.abc import Iterator
from functools import lru_cache
from typing import Literal, Protocol, cast

import re2

from quail.contracts import JSONValue, QuailError, canonical_json, decode_json, text_value

type Encoding = Literal["text", "number", "json", "bool"]
type SQLValue = str | int | float | bytes | None


class Match(Protocol):
    def group(self, index: int = 0) -> str: ...


class Pattern(Protocol):
    def search(self, text: str) -> Match | None: ...
    def finditer(self, text: str) -> Iterator[Match]: ...
    def sub(self, replacement: str, text: str) -> str: ...


@lru_cache(maxsize=128)
def regex(pattern: str, flags: int) -> Pattern:
    if not isinstance(pattern, str) or type(flags) not in {int, re.RegexFlag}:
        raise QuailError("Regex needs a text pattern and re.I, re.M, or re.S flags")
    if int(flags) & ~int(re.I | re.M | re.S):
        raise QuailError("Unsupported regex flags", "Only re.I, re.M, and re.S are supported")
    options = re2.Options()
    options.case_sensitive = not bool(flags & re.I)
    options.log_errors = False
    prefix = ("(?m)" if flags & re.M else "") + ("(?s)" if flags & re.S else "")
    try:
        # google-re2's extension has no stubs; its small native surface ends here.
        return cast(Pattern, re2.compile(prefix + pattern, options=options))
    except re2.error as error:
        raise QuailError(f"Invalid RE2 pattern: {error}") from error


def decode(value: SQLValue, encoding: str) -> JSONValue:
    if value is None:
        return None
    if encoding == "json":
        if not isinstance(value, str):
            raise TypeError("JSON SQL values must be text")
        return decode_json(value)
    if encoding == "bool":
        return bool(value)
    if isinstance(value, bytes):
        raise TypeError("Packed vectors are not scalar language values")
    return value


def encode(value: JSONValue, encoding: str) -> SQLValue:
    if value is None:
        return None
    if encoding == "json":
        return canonical_json(value)
    if encoding == "number":
        number = numeric(value)
        # SQLite's integer channel is signed 64-bit; larger numeric results use REAL.
        if isinstance(number, int) and not -(2**63) <= number < 2**63:
            try:
                result = float(number)
            except OverflowError:
                return None
            return result if math.isfinite(result) else None
        return number
    if isinstance(value, (str, int, float)):
        return value
    raise TypeError(f"Cannot encode {type(value).__name__} as {encoding}")


def numeric(value: JSONValue) -> int | float | None:
    if isinstance(value, (int, float)):
        return value if isinstance(value, int) or math.isfinite(value) else None
    if isinstance(value, str):
        try:
            result = float(value)
            return result if math.isfinite(result) else None
        except (ValueError, OverflowError):
            pass
    return None


def compare(operation: str, left: JSONValue, right: JSONValue, literal_side: int = 0) -> bool:
    """Absence and literal numeric conversion precede ordinary Python equality."""
    literal_value = left if literal_side == 1 else right
    if literal_side and literal_value is None:
        other = right if literal_side == 1 else left
        return (other is None) if operation == "eq" else (other is not None and operation == "ne")
    if literal_side and isinstance(literal_value, (int, float)):
        if literal_side == 1:
            right = numeric(right)
        else:
            left = numeric(left)
    if left is None or right is None:
        return False
    if operation == "eq":
        return left == right
    if operation == "ne":
        return left != right
    if isinstance(left, (list, dict)) or isinstance(right, (list, dict)):
        return False
    if isinstance(left, str) and isinstance(right, str):
        order = (left > right) - (left < right)
    elif isinstance(left, (int, float)) and isinstance(right, (int, float)):
        order = (left > right) - (left < right)
    else:
        return False
    return {"lt": order < 0, "le": order <= 0, "gt": order > 0, "ge": order >= 0}[operation]


def _comparison(op: str, left: SQLValue, le: str, right: SQLValue, re: str, side: int) -> int:
    return int(compare(op, decode(left, le), decode(right, re), side))


def _arithmetic(op: str, left: SQLValue, le: str, right: SQLValue, re: str) -> SQLValue:
    a, b = numeric(decode(left, le)), numeric(decode(right, re))
    if a is None or b is None:
        return None
    try:
        match op:
            case "add":
                result = a + b
            case "subtract":
                result = a - b
            case "multiply":
                result = a * b
            case "divide":
                result = a / b
            case _:
                raise ValueError(f"Unknown arithmetic operation: {op}")
        return encode(result, "number")
    except (ZeroDivisionError, OverflowError):
        return None


def _value(op: str, raw: SQLValue, encoding: str, output: str, *arguments: SQLValue) -> SQLValue:
    value = decode(raw, encoding)
    if op == "isin":
        choices = decode_json(str(arguments[0]))
        assert isinstance(choices, list)
        return int(any(compare("eq", value, choice, 2) for choice in choices))
    if op == "contains":
        choice = decode_json(str(arguments[0]))
        if isinstance(value, str):
            return int(isinstance(choice, str) and choice in value)
        if isinstance(value, list):
            return int(choice in value)
        return int(isinstance(value, dict) and isinstance(choice, str) and choice in value)
    if value is None:
        return None
    result: JSONValue
    match op:
        case "text":
            result = text_value(value)
        case "number":
            result = numeric(value)
        case "length":
            result = len(value) if isinstance(value, (str, list, dict)) else None
        case "slice":
            start, end = arguments
            assert start is None or isinstance(start, int)
            assert end is None or isinstance(end, int)
            result = value[start:end] if isinstance(value, (str, list)) else None
        case "lower" | "upper" | "strip":
            text = text_value(value)
            assert text is not None
            if op == "lower":
                result = text.lower()
            elif op == "upper":
                result = text.upper()
            else:
                result = text.strip()
        case "search" | "findall" | "sub":
            flags = arguments[-1]
            assert isinstance(flags, int)
            compiled = regex(str(arguments[0]), flags)
            text = text_value(value)
            assert text is not None
            if op == "search":
                match = compiled.search(text)
                result = match.group(0) if match else None
            elif op == "findall":
                result = [match.group(0) for match in compiled.finditer(text)]
            elif isinstance(value, list):
                result = [
                    None
                    if item is None
                    else compiled.sub(str(arguments[1]), text_value(item) or "")
                    for item in value
                ]
            else:
                result = compiled.sub(str(arguments[1]), text)
        case _:
            raise ValueError(f"Unknown value operation: {op}")
    return encode(result, output)


def _random(salt: str, entry: str) -> float:
    digest = hashlib.sha256((salt + "\0" + entry).encode("utf-8")).digest()
    return (int.from_bytes(digest[:8], "big") >> 11) / 2**53


def register(connection: sqlite3.Connection) -> None:
    connection.create_function("q_compare", 6, _comparison, deterministic=True)
    connection.create_function("q_arithmetic", 5, _arithmetic, deterministic=True)
    connection.create_function("q_value", -1, _value, deterministic=True)
    connection.create_function(
        "q_json", 2, lambda value, kind: encode(decode(value, kind), "json"), deterministic=True
    )
    connection.create_function("q_random", 2, _random, deterministic=True)
