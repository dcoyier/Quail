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
    "quail_setup → quail_get_dataset_info (when needed) → quail_exec."
)

_SESSION_RULES = (
    "Sessions are workspace-scoped: after quail_switch_workspace, call "
    "quail_setup again (or quail_start_session) and do not reuse a prior session_id. "
    "Run only one quail_exec in flight per session_id (serial chaining is fine; "
    "parallel execs on the same session fail with session_busy)."
)

_CLERK_SESSION_RULES = (
    "Sessions are workspace-scoped and owned by the user who created them: after "
    "quail_switch_workspace, call quail_setup again (or quail_start_session) and "
    "do not reuse a prior session_id. Do not use another user's session_id. "
    "Run only one quail_exec in flight per session_id (serial chaining is fine; "
    "parallel execs on the same session fail with session_busy)."
)


def unrestricted_instructions(workspace_id: str) -> str:
    return (
        f"Quail MCP for fixed workspace `{workspace_id}` (unrestricted; no sign-in).\n"
        "\n"
        "Workflow: quail_setup → quail_get_dataset_info(dataset_id) → "
        "quail_exec(session_id, dataset_id, code). Reuse the same session_id "
        "serially within this workspace.\n"
        "\n"
        f"{_SESSION_RULES}\n"
        "\n"
        "quail_setup returns analysis-language docs, the dataset catalog, and a "
        "fresh session_id. Call it once at cold start (and again after a workspace "
        "switch). Prefer it over separately calling quail_get_api_docs, "
        "quail_list_datasets, and quail_start_session unless you need a refresh "
        "or only one of those pieces.\n"
        "\n"
        "Dataset-specific guidance comes from quail_get_dataset_info, unless "
        "quail_setup already included documentation for that dataset_id — then "
        "do not call quail_get_dataset_info again for those docs.\n"
        "\n"
        'quail_exec success is {"printed_output"}; failure is a diagnostic '
        'and commits nothing. time_window is "standard" (30s wall / 15s CPU) or '
        '"extended" (100s wall / 60s CPU); worker RSS is capped at 256 MiB.\n'
        "\n"
        "provide_feedback for friction or improvements (low bar for entry) — not for "
        "analysis results. Optional category, session_id, dataset_id.\n"
        "\n"
        "quail_export_csv(session_id, dataset_id) writes source columns plus this "
        "session's tags to a new CSV on this machine (path in the result). "
        "Lexical/Semantic on analysis tags materializes cells; after those columns "
        "are source, scoring uses the process-warmed path. This tool does not "
        "reprocess or edit quail.toml. Keep the same dataset id, point source at "
        "the export, stop quail run, quail process, quail run, then "
        "quail_start_session. A new id is a different dataset. The path is on "
        "the machine running quail run (not a download). Do not overlap "
        "quail_export_csv with quail_exec on the same session_id (session_busy). "
        "Do not export to copy source fields, for one-off filters, or to persist "
        "bindings."
    )


def clerk_instructions(*, locked: bool = False) -> str:
    base = (
        "This Quail deployment uses Clerk identity on one MCP URL. A valid Clerk "
        "token proves who you are; the operator TOML allowlist decides whether you "
        "may call tools. Tokens must be for this Clerk application "
        "(authorized parties). Advertised OAuth scopes are for client compatibility; "
        "Quail does not enforce per-scope grants from the token.\n"
        "Workspaces contain datasets. Bind a workspace before dataset, session, or exec "
        "tools: call quail_list_workspaces, then quail_switch_workspace (or rely on a "
        "bound default when the connection is already bound). After binding, call "
        "quail_setup once, then quail_get_dataset_info when needed, then quail_exec. "
        "Prefer quail_setup over separately calling quail_get_api_docs, "
        "quail_list_datasets, and quail_start_session unless you need a refresh or "
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
