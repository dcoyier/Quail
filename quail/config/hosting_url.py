"""Public base URL helpers for hosting / MCP OAuth resource identity."""

from __future__ import annotations

from urllib.parse import urlparse

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def is_loopback_host(host: str) -> bool:
    """True for 127.0.0.1, localhost, and ::1 (bracket form ok)."""

    raw = host.strip().lower()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    return raw in _LOOPBACK_HOSTS


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
    """Derive a local public base URL from hosting bind/port."""

    host = "127.0.0.1" if bind in {"0.0.0.0", "::", "[::]"} else bind
    return f"http://{host}:{port}"
