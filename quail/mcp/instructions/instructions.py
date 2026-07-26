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
    "quail_get_api_docs / quail_list_datasets / quail_start_session / quail_exec."
)

_SESSION_RULES = (
    "Sessions are workspace-scoped: after quail_switch_workspace, call "
    "quail_start_session again and do not reuse a prior session_id. "
    "Run only one quail_exec in flight per session_id (serial chaining is fine; "
    "parallel execs on the same session fail with session_busy)."
)


def unrestricted_instructions(workspace_id: str) -> str:
    return (
        f"Quail MCP for fixed workspace `{workspace_id}` (unrestricted; no sign-in).\n"
        "\n"
        "Workflow: quail_get_api_docs → quail_list_datasets → "
        "quail_start_session → quail_get_dataset_info(dataset_id) → "
        "quail_exec(session_id, dataset_id, code). Reuse the same session_id "
        "serially within this workspace.\n"
        "\n"
        f"{_SESSION_RULES}\n"
        "\n"
        "quail_get_api_docs returns the analysis language for code inside "
        "quail_exec.\n"
        "Dataset-specific guidance comes from quail_get_dataset_info.\n"
        "\n"
        'quail_exec success is {"printed_output"}; failure is a diagnostic '
        'and commits nothing. time_window is "standard" (30s wall / 15s CPU) or '
        '"extended" (100s wall / 60s CPU); worker RSS is capped at 256 MiB.\n'
        "\n"
        "provide_feedback for friction or improvements (low bar for entry) — not for "
        "analysis results. Optional category, session_id, dataset_id."
    )


def clerk_instructions(*, locked: bool = False) -> str:
    base = (
        "This Quail deployment uses Clerk auth on one MCP URL.\n"
        "Workspaces contain datasets. Bind a workspace before dataset, session, or exec "
        "tools: call quail_list_workspaces, then quail_switch_workspace (or rely on a "
        "bound default when the connection is already bound). After binding, "
        "quail_list_datasets, quail_get_dataset_info, quail_start_session, and "
        "quail_exec only see that workspace's data. If a workspace is already bound "
        "(including via default_workspace), keep it unless the user asks to change "
        "workspace or the task clearly requires another one.\n"
        f"{_SESSION_RULES}\n"
        "Call quail_get_api_docs before writing quail_exec code. Use provide_feedback "
        "for friction or improvements."
    )
    if locked:
        return f"{base}\n\n{_LOCKED_ADDENDUM}"
    return base
