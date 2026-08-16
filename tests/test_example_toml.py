"""Example TOMLs parse, and every template key is present in the file."""

from __future__ import annotations

from pathlib import Path

from quail.config import load_config

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples"

# Every legal key (and the clerk-only / unrestricted-only flags that must be
# named so a reader does not have to look elsewhere).
_UNRESTRICTED_KEYS = (
    "database",
    "feedback",
    "search_database",
    "mode",
    "workspace",
    "bind",
    "port",
    "public_base_url",
    "max_concurrent_executions",
    "allow_public_unrestricted",
    "allow_insecure_http",
    "include_dataset_docs_in_setup",
    "embed_batch_size",
    "max_concurrent_embed_requests",
    "providers.ollama",
    "providers.openrouter",
    "api_key",
    "env:",
    "[[datasets]]",
    "[datasets.embedding]",
    "[datasets.lexical]",
    "[[extensions]]",
    "[[connectors]]",
    "[connectors.config]",
    "[[connectors.datasets]]",
    "process --clear",
)

_CLERK_KEYS = (
    "database",
    "feedback",
    "search_database",
    "mode",
    "clerk_domain",
    "clerk_authorized_parties",
    "bind",
    "port",
    "public_base_url",
    "max_concurrent_executions",
    "allow_insecure_http",
    "allow_public_unrestricted",
    "include_dataset_docs_in_setup",
    "embed_batch_size",
    "max_concurrent_embed_requests",
    "providers.ollama",
    "providers.openrouter",
    "api_key",
    "[[extensions]]",
    "[[workspaces]]",
    "[[workspaces.datasets]]",
    "[workspaces.datasets.embedding]",
    "[workspaces.datasets.lexical]",
    "[[workspaces.connectors]]",
    "[workspaces.connectors.config]",
    "[[workspaces.connectors.datasets]]",
    "[[users]]",
    "clerk_user_id",
    "default_workspace",
    "lock_workspace",
    "auth.workspace",
    "root [[datasets]]",
    "root [[connectors]]",
    "env:",
    "process --clear",
)


def test_example_unrestricted_toml_parses_and_lists_template_keys() -> None:
    path = _EXAMPLES / "quail.toml"
    config = load_config(path)
    assert config.auth_mode == "unrestricted"
    assert config.workspace_id == "local"
    assert config.datasets[0].dataset_id == "notes"
    text = path.read_text(encoding="utf-8")
    missing = [key for key in _UNRESTRICTED_KEYS if key not in text]
    assert missing == []


def test_example_clerk_toml_parses_and_lists_template_keys() -> None:
    path = _EXAMPLES / "quail.clerk.toml"
    config = load_config(path)
    assert config.auth_mode == "clerk"
    assert config.workspace_id is None
    assert {spec.workspace_id for spec in config.workspaces} == {"acme", "labs"}
    assert config.users[0].user_id == "alice"
    text = path.read_text(encoding="utf-8")
    missing = [key for key in _CLERK_KEYS if key not in text]
    assert missing == []
