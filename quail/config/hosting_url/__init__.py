"""Public exports for ``quail.config.hosting_url``."""

from .hosting_url import (
    default_public_base_url,
    is_loopback_host,
    is_loopback_public_base_url,
    is_wildcard_bind,
    normalize_public_base_url,
)

__all__ = [
    "default_public_base_url",
    "is_loopback_host",
    "is_loopback_public_base_url",
    "is_wildcard_bind",
    "normalize_public_base_url",
]
