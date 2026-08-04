"""MCP server instruction templates by auth mode."""

from __future__ import annotations

UNBOUND_REPAIR_HINT = (
    "Call quail_list_workspaces, then quail_switch_workspace, or ask the operator "
    "to set default_workspace in quail.toml."
)

LOCK_REPAIR_HINT = (
    "Workspace is locked in TOML to your default_workspace; continue without switching."
)

_LOCKED_ADDENDUM = (
    "Your workspace is pinned by the operator (lock_workspace). It is already bound "
    "to your default_workspace. Do not call quail_list_workspaces or "
    "quail_switch_workspace; they will not change the workspace. Proceed with "
    "quail_setup → quail_get_dataset_info (when needed) → search."
)

_SESSION_RULES = (
    "Sessions are workspace-scoped: after quail_switch_workspace, call "
    "quail_setup again (or quail_start_session) and do not reuse a prior session_id. "
    "Run only one search in flight per session_id (serial chaining is fine; "
    "parallel search on the same session fails with session_busy). "
    "get_entry may run without that search lock."
)

_CLERK_SESSION_RULES = (
    "Sessions are workspace-scoped and owned by the user who created them: after "
    "quail_switch_workspace, call quail_setup again (or quail_start_session) and "
    "do not reuse a prior session_id. Do not use another user's session_id. "
    "Run only one search in flight per session_id (serial chaining is fine; "
    "parallel search on the same session fails with session_busy). "
    "get_entry may run without that search lock."
)


def unrestricted_instructions(workspace_id: str) -> str:
    return (
        f"Quail MCP for fixed workspace `{workspace_id}` (unrestricted; no sign-in).\n"
        "\n"
        "Workflow: quail_setup → quail_get_dataset_info(dataset_id) → "
        "search(session_id, dataset_id, query) → optional "
        "get_entry(session_id, result_handle). "
        "Answer from retrieved evidence in the chat turn. "
        "Reuse the same session_id serially within this workspace.\n"
        "\n"
        f"{_SESSION_RULES}\n"
        "\n"
        "quail_setup returns the dataset catalog and a fresh session_id "
        "(no analysis-language docs). Call it once at cold start (and again after "
        "a workspace switch). Prefer it over separately calling "
        "quail_list_datasets and quail_start_session unless you need a refresh "
        "or only one of those pieces.\n"
        "\n"
        "Dataset-specific guidance comes from quail_get_dataset_info, unless "
        "quail_setup already included documentation for that dataset_id — then "
        "do not call quail_get_dataset_info again for those docs.\n"
        "\n"
        "search returns ranked entries for a natural-language query "
        "(optional top_k, default 8, max 20). Each hit has a canonical entry_id "
        "for citation and an opaque result_handle for get_entry. get_entry only "
        "accepts a result_handle returned by search in the same session; rerun "
        "search after starting a new session. Do not invent evidence beyond tool results.\n"
        "\n"
        "provide_feedback for friction or improvements (low bar for entry) — not for "
        "retrieval answers. Optional category, session_id, dataset_id."
    )


def clerk_instructions(*, locked: bool = False) -> str:
    base = (
        "This Quail deployment uses Clerk identity on one MCP URL. A valid Clerk "
        "token proves who you are; the operator TOML allowlist decides whether you "
        "may call tools. Tokens must be for this Clerk application "
        "(authorized parties). Advertised OAuth scopes are for client compatibility; "
        "Quail does not enforce per-scope grants from the token.\n"
        "Workspaces contain datasets. Bind a workspace before dataset, session, or "
        "search tools: call quail_list_workspaces, then quail_switch_workspace (or rely "
        "on a bound default when the connection is already bound). After binding, call "
        "quail_setup once, then quail_get_dataset_info when needed, then search "
        "(optional get_entry with a same-session result_handle). Prefer quail_setup "
        "over separately calling "
        "quail_list_datasets and quail_start_session unless you need a refresh or "
        "only one of those pieces. If a workspace is already bound "
        "(including via default_workspace), keep it unless the user asks to change "
        "workspace or the task clearly requires another one.\n"
        f"{_CLERK_SESSION_RULES}\n"
        "Dataset-specific guidance comes from quail_get_dataset_info, unless "
        "quail_setup already included documentation for that dataset_id — then "
        "do not call quail_get_dataset_info again for those docs. "
        "Use provide_feedback for friction or improvements."
    )
    if locked:
        return f"{base}\n\n{_LOCKED_ADDENDUM}"
    return base
