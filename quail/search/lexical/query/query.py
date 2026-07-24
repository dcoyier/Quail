"""Lexical query parse, compile, and text preparation for Turso native FTS."""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from enum import Enum

from quail.analysis.errors import QuailRuntimeError

MAX_TERM_BYTES = 40
MAX_QUERY_BYTES = 64 * 1024
MAX_QUERY_LEAVES = 1_024
MAX_PHRASE_TOKENS = 4_096
_HASHED_TERM_PREFIX = "!sha256:"
MATCH_NONE_TERM = "!quail:no-match!"


class LeafKind(Enum):
    TERM = "term"
    PHRASE = "phrase"
    PREFIX = "prefix"


@dataclass(frozen=True, slots=True)
class Leaf:
    kind: LeafKind
    terms: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BooleanExpression:
    required: tuple[Leaf, ...]
    excluded: tuple[Leaf, ...]


@dataclass(frozen=True, slots=True)
class OrExpression:
    expressions: tuple[Expression, ...]


Expression = Leaf | BooleanExpression | OrExpression


class _TokenKind(Enum):
    AND = "AND"
    NOT = "NOT"
    LEAF = "leaf"


@dataclass(frozen=True, slots=True)
class _Token:
    kind: _TokenKind
    leaf: Leaf | None = None


@dataclass(slots=True)
class _Budget:
    leaves: int = 0
    phrase_tokens: int = 0

    def charge_leaf(self) -> None:
        self.leaves += 1
        if self.leaves > MAX_QUERY_LEAVES:
            raise QuailRuntimeError(
                f"Lexical request exceeds the {MAX_QUERY_LEAVES}-leaf query limit"
            )

    def charge_phrase(self, count: int) -> None:
        self.phrase_tokens += count
        if self.phrase_tokens > MAX_PHRASE_TOKENS:
            raise QuailRuntimeError(
                f"Lexical request exceeds the {MAX_PHRASE_TOKENS}-token phrase limit"
            )


def prepare_text(text: str) -> tuple[str, tuple[tuple[str, str], ...]]:
    terms: list[tuple[str, str]] = []
    for source in _token_sources(text):
        normalized = normalize_term(source)
        terms.append((normalized, _index_normalized_term(normalized)))
    output = tuple(terms)
    return " ".join(indexed for _, indexed in output), output


def prepare_prefix_text(
    terms: tuple[tuple[str, str], ...],
) -> tuple[str, tuple[tuple[str, str], ...]]:
    prefixes = tuple(_prefix_normalized_term(normalized) for normalized, _ in terms)
    return " ".join(prefixes), tuple((prefix, prefix) for prefix in prefixes)


def tokenize(text: str) -> tuple[str, ...]:
    prepared, _ = prepare_text(text)
    return tuple(prepared.split())


def normalize_term(source: str) -> str:
    normalized = _stream_safe_nfc(source)
    lowered = "".join(character.lower() for character in normalized)
    return _stream_safe_nfc(lowered)


def parse_queries(queries: tuple[str, ...]) -> tuple[Expression, ...]:
    if len(queries) > MAX_QUERY_LEAVES:
        raise QuailRuntimeError(
            f"Lexical request contains more than the {MAX_QUERY_LEAVES}-query limit"
        )
    query_bytes = sum(len(query.encode("utf-8")) for query in queries)
    if query_bytes > MAX_QUERY_BYTES:
        raise QuailRuntimeError(f"Lexical request exceeds the {MAX_QUERY_BYTES}-byte query limit")
    budget = _Budget()
    return tuple(_parse(query, budget) for query in queries)


def compile_query(expression: Expression, prefixes: dict[str, tuple[str, ...]]) -> str:
    if isinstance(expression, Leaf):
        return _compile_leaf(expression, prefixes)
    if isinstance(expression, BooleanExpression):
        required = " AND ".join(_compile_leaf(leaf, prefixes) for leaf in expression.required)
        excluded = "".join(f" NOT {_compile_leaf(leaf, prefixes)}" for leaf in expression.excluded)
        return f"({required}{excluded})"
    return "(" + " OR ".join(compile_query(item, prefixes) for item in expression.expressions) + ")"


def collect_prefixes(expressions: tuple[Expression, ...]) -> tuple[str, ...]:
    prefixes: set[str] = set()

    def collect(expression: Expression) -> None:
        if isinstance(expression, Leaf):
            if expression.kind is LeafKind.PREFIX:
                prefixes.add(expression.terms[0])
            return
        if isinstance(expression, BooleanExpression):
            for leaf in (*expression.required, *expression.excluded):
                if leaf.kind is LeafKind.PREFIX:
                    prefixes.add(leaf.terms[0])
            return
        for item in expression.expressions:
            collect(item)

    for expression in expressions:
        collect(expression)
    return tuple(sorted(prefixes))


def quote_term(value: str) -> str:
    if '"' in value or "\\" in value:
        raise QuailRuntimeError("Lexical term cannot be represented safely")
    return f'"{value}"'


def _parse(query: str, budget: _Budget) -> Expression:
    tokens = _lex(query, budget)
    if not tokens:
        raise QuailRuntimeError("Lexical query must contain a term or phrase")
    if tokens[0].kind is _TokenKind.NOT:
        raise QuailRuntimeError("Pure-negative lexical queries are not supported")
    parser = _Parser(tokens)
    expression = parser.parse_or()
    if parser.cursor != len(tokens):
        raise parser.unexpected_token()
    return expression


def _lex(query: str, budget: _Budget) -> tuple[_Token, ...]:
    if "\x00" in query:
        raise QuailRuntimeError("Lexical queries cannot contain NUL")
    tokens: list[_Token] = []
    cursor = 0
    while cursor < len(query):
        character = query[cursor]
        if character.isspace():
            cursor += 1
            continue
        if character == '"':
            leaf, cursor = _lex_phrase(query, cursor, budget)
            if cursor < len(query) and not query[cursor].isspace():
                raise QuailRuntimeError("Quoted phrases must be separated from adjacent terms")
            tokens.append(_Token(_TokenKind.LEAF, leaf))
            continue
        start = cursor
        while cursor < len(query) and not query[cursor].isspace() and query[cursor] != '"':
            cursor += 1
        if cursor < len(query) and query[cursor] == '"':
            raise QuailRuntimeError("Quoted phrases must be separated from adjacent terms")
        tokens.append(_lex_word(query[start:cursor], budget))
    return tuple(tokens)


def _lex_phrase(query: str, quote_start: int, budget: _Budget) -> tuple[Leaf, int]:
    content_start = quote_start + 1
    content_end = query.find('"', content_start)
    if content_end < 0:
        raise QuailRuntimeError("Lexical query has an unclosed quote")
    content = query[content_start:content_end]
    if "*" in content:
        raise QuailRuntimeError("Wildcards are only supported after an unquoted term")
    terms = tokenize(content)
    if not terms:
        raise QuailRuntimeError("Lexical phrase must contain an indexed term")
    budget.charge_phrase(len(terms))
    budget.charge_leaf()
    return Leaf(LeafKind.PHRASE, terms), content_end + 1


def _lex_word(word: str, budget: _Budget) -> _Token:
    if word == "AND":
        return _Token(_TokenKind.AND)
    if word == "NOT":
        return _Token(_TokenKind.NOT)
    if word == "OR":
        raise QuailRuntimeError("Explicit OR is unsupported; separate terms with spaces")

    wildcard_count = word.count("*")
    is_prefix = word.endswith("*")
    if word == "*":
        raise QuailRuntimeError("Bare wildcard is not supported")
    if wildcard_count > int(is_prefix):
        raise QuailRuntimeError("Wildcards are only supported once at the end of a term")
    source = word[:-1] if is_prefix else word
    if not source:
        raise QuailRuntimeError("Bare wildcard is not supported")
    if not _is_word_source(source):
        raise QuailRuntimeError(f"Unsupported lexical query construct in {word!r}")
    analyzed = tokenize(source)
    if len(analyzed) != 1:
        raise QuailRuntimeError(
            f"Lexical term {source!r} is removed by the current lexical tokenizer"
        )
    normalized = analyzed[0]
    if is_prefix and normalized.startswith(_HASHED_TERM_PREFIX):
        raise QuailRuntimeError(
            f"Lexical prefixes must be shorter than {MAX_TERM_BYTES} normalized UTF-8 bytes"
        )
    budget.charge_leaf()
    kind = LeafKind.PREFIX if is_prefix else LeafKind.TERM
    return _Token(_TokenKind.LEAF, Leaf(kind, (normalized,)))


def _compile_leaf(leaf: Leaf, prefixes: dict[str, tuple[str, ...]]) -> str:
    if leaf.kind is LeafKind.PHRASE:
        return quote_term(" ".join(leaf.terms))
    if leaf.kind is LeafKind.TERM:
        return quote_term(leaf.terms[0])
    expanded = prefixes[leaf.terms[0]]
    if not expanded:
        return quote_term(MATCH_NONE_TERM)
    return "(" + " OR ".join(quote_term(term) for term in expanded) + ")"


def _token_sources(text: str) -> tuple[str, ...]:
    output: list[str] = []
    cursor = 0
    while cursor < len(text):
        if not text[cursor].isalnum():
            cursor += 1
            continue
        start = cursor
        cursor += 1
        while cursor < len(text):
            character = text[cursor]
            if not character.isalnum() and not _is_combining_mark(character):
                break
            cursor += 1
        output.append(text[start:cursor])
    return tuple(output)


def _stream_safe_nfc(source: str) -> str:
    output: list[str] = []
    nonstarters = 0
    for character in unicodedata.normalize("NFD", source):
        if unicodedata.combining(character) == 0:
            nonstarters = 0
        else:
            if nonstarters == 30:
                output.append("\N{COMBINING GRAPHEME JOINER}")
                nonstarters = 0
            nonstarters += 1
        output.append(character)
    return unicodedata.normalize("NFC", "".join(output))


def _index_normalized_term(normalized: str) -> str:
    encoded = normalized.encode("utf-8")
    if len(encoded) < MAX_TERM_BYTES:
        return normalized
    return _HASHED_TERM_PREFIX + hashlib.sha256(encoded).hexdigest()


def _prefix_normalized_term(normalized: str) -> str:
    output: list[str] = []
    byte_count = 0
    for character in normalized:
        character_bytes = len(character.encode("utf-8"))
        if byte_count + character_bytes >= MAX_TERM_BYTES:
            break
        output.append(character)
        byte_count += character_bytes
    return "".join(output)


def _is_word_source(source: str) -> bool:
    return (
        bool(source)
        and source[0].isalnum()
        and all(character.isalnum() or _is_combining_mark(character) for character in source[1:])
    )


def _is_combining_mark(character: str) -> bool:
    return unicodedata.category(character).startswith("M")


class _Parser:
    def __init__(self, tokens: tuple[_Token, ...]) -> None:
        self.tokens = tokens
        self.cursor = 0

    def parse_or(self) -> Expression:
        expressions = [self.parse_explicit()]
        while self.cursor < len(self.tokens) and self.tokens[self.cursor].kind is _TokenKind.LEAF:
            expressions.append(self.parse_explicit())
        if len(expressions) == 1:
            return expressions[0]
        return OrExpression(tuple(expressions))

    def parse_explicit(self) -> Expression:
        required = [self.parse_operand()]
        excluded: list[Leaf] = []
        while self.cursor < len(self.tokens):
            operator = self.tokens[self.cursor].kind
            if operator not in {_TokenKind.AND, _TokenKind.NOT}:
                break
            self.cursor += 1
            try:
                operand = self.parse_operand()
            except QuailRuntimeError as error:
                raise QuailRuntimeError(
                    f"Lexical operator {operator.value} is missing its right operand"
                ) from error
            if operator is _TokenKind.AND:
                required.append(operand)
            else:
                excluded.append(operand)
        if len(required) == 1 and not excluded:
            return required[0]
        return BooleanExpression(tuple(required), tuple(excluded))

    def parse_operand(self) -> Leaf:
        if self.cursor >= len(self.tokens):
            raise QuailRuntimeError("Lexical query must contain a term or phrase")
        token = self.tokens[self.cursor]
        if token.kind is _TokenKind.AND:
            raise QuailRuntimeError("Lexical operator AND is missing its left operand")
        if token.kind is _TokenKind.NOT:
            raise QuailRuntimeError("Lexical operator NOT is missing its left operand")
        self.cursor += 1
        assert token.leaf is not None
        return token.leaf

    def unexpected_token(self) -> QuailRuntimeError:
        token = self.tokens[self.cursor]
        if token.kind is _TokenKind.AND:
            return QuailRuntimeError("Lexical operator AND is missing its left operand")
        if token.kind is _TokenKind.NOT:
            return QuailRuntimeError("Lexical operator NOT is missing its left operand")
        return QuailRuntimeError("Invalid lexical query expression")
