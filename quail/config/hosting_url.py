"""Public base URL helpers for hosting / MCP OAuth resource identity."""

from __future__ import annotations

from urllib.parse import urlparse

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_WILDCARD_BINDS = frozenset({"0.0.0.0", "::", "[::]"})


def is_loopback_host(host: str) -> bool:
    """True for 127.0.0.1, localhost, and ::1 (bracket form ok)."""

    raw = host.strip().lower()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    return raw in _LOOPBACK_HOSTS


def is_wildcard_bind(bind: str) -> bool:
    """True for 0.0.0.0 / :: binds that listen on every interface."""

    return bind.strip().lower() in _WILDCARD_BINDS


def is_loopback_public_base_url(url: str) -> bool:
    """True when the origin host is loopback."""

    host = urlparse(url).hostname
    return host is not None and is_loopback_host(host)


def normalize_public_base_url(value: str) -> str:
    """Require an absolute http(s) origin (no path, query, or fragment)."""

    raw = value.strip()
    if not raw:
        raise ValueError("public_base_url cannot be empty")
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("public_base_url must be an absolute http(s) URL")
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise ValueError("public_base_url must be an origin only (no path, query, or fragment)")
    return f"{parsed.scheme}://{parsed.netloc}"


def default_public_base_url(*, bind: str, port: int) -> str:
    """Derive a local public base URL from hosting bind/port.

    Wildcard binds cannot imply a loopback origin; callers must set
    ``public_base_url`` explicitly.
    """

    if is_wildcard_bind(bind):
        raise ValueError(
            "public_base_url is required when bind is 0.0.0.0 or :: "
            "(wildcard bind cannot imply a loopback origin)"
        )
    return f"http://{_origin_host(bind)}:{port}"


def _origin_host(bind: str) -> str:
    raw = bind.strip()
    if raw.startswith("[") and raw.endswith("]"):
        return raw
    if ":" in raw:
        return f"[{raw}]"
    return raw
