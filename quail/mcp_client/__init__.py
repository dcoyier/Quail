"""Public exports for ``quail.mcp_client``."""

from .mcp_client import (
    DEFAULT_URL,
    call_tool,
    list_tools,
    load_arguments,
    main,
)

__all__ = [
    "DEFAULT_URL",
    "call_tool",
    "list_tools",
    "load_arguments",
    "main",
]
