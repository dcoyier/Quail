"""Process-local sticky workspace binding keyed by opaque caller ids.

Clerk keys are user:{clerk_user_id}: every MCP connection as that user on
this process shares one bind.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from quail.config.models import UserSpec


@dataclass
class StickyWorkspaceStore:
    """Map opaque keys to bound workspace ids."""

    _bound: dict[str, str] = field(default_factory=dict)
    _seen: set[str] = field(default_factory=set)

    def active(self, connection_key: str) -> str | None:
        return self._bound.get(connection_key)

    def ensure_initial_bind(self, connection_key: str, user: UserSpec) -> str | None:
        """Apply default_workspace the first time a key is seen; leave unbound otherwise."""

        if connection_key not in self._seen:
            self._seen.add(connection_key)
            if user.default_workspace is not None:
                self._bound[connection_key] = user.default_workspace
        return self._bound.get(connection_key)

    def bind(self, connection_key: str, workspace_id: str) -> str:
        self._seen.add(connection_key)
        self._bound[connection_key] = workspace_id
        return workspace_id
