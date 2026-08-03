#!/usr/bin/env python3
"""Cursor-only in-process Lexical/Semantic smoke (exec_script).

Do NOT ship in ChatGPT eval wheel. Do NOT run during scored trials.

Requires: Ollama embeddinggemma:300m-qat-q8_0 on 127.0.0.1:11434, and `quail`.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--config",
        type=Path,
        required=True,
        help="Absolute path to assembled pack quail.toml",
    )
    p.add_argument(
        "--query",
        default="cognitive dissonance and attitude change",
        help="Query text for Lexical + Semantic retrieve",
    )
    p.add_argument("--limit", type=int, default=5)
    args = p.parse_args()

    config_path = args.config.expanduser().resolve()
    if not config_path.is_file():
        print(f"missing config: {config_path}", file=sys.stderr)
        return 2

    from quail.analysis.exec_host import exec_script
    from quail.config import load_config
    from quail.datasets import open_core_db
    from quail.run.apply import import_declared_datasets
    from quail.run.process import assert_search_warm
    from quail.search.runtime import search_runtime_from_config
    from quail.session import create_session

    config = load_config(config_path)
    db = open_core_db(config.database)
    runtime = search_runtime_from_config(config)
    if runtime is None:
        print("search_database not configured", file=sys.stderr)
        return 2
    try:
        refs = import_declared_datasets(config, db, activate=True)
        assert_search_warm(db, config, refs)
        workspace_id = config.datasets[0].workspace_id
        dataset_id = config.datasets[0].dataset_id
        session = create_session(db, workspace_id)
        # Keep quail_exec code free of reserved names (entries/fields/…).
        code = f"""
query = {args.query!r}
score = Expression(Field("body"), Semantic(query))
matching = G0.where(score > 0)
rank = Ranking(expression=score)
hits = retrieve(group=matching, rank=rank, limit={args.limit})
vals = retrieve(unit=score, group=matching, rank=rank, limit={args.limit})
print("SEMANTIC")
for i in range(len(hits)):
    body = hits[i].value(Field("body")) or ""
    print(hits[i].id, round(vals[i], 4), body[:160].replace("\\n", " "))
lex_score = Expression(Field("body"), Lexical(query))
lex_matching = G0.where(lex_score > 0)
lex_rank = Ranking(expression=lex_score)
lex_hits = retrieve(group=lex_matching, rank=lex_rank, limit=3)
print("LEXICAL", len(lex_hits))
"""
        outcome = exec_script(
            db,
            session_id=session.id,
            dataset_id=dataset_id,
            expected_revision=session.state_revision,
            code=code,
            search_runtime=runtime,
            time_window="extended",
        )
        sys.stdout.write(outcome.printed_output)
        if "SEMANTIC" not in outcome.printed_output:
            print("SMOKE_SEMANTIC_FAIL missing SEMANTIC section", file=sys.stderr)
            return 1
        print("SMOKE_SEMANTIC_OK")
        return 0
    finally:
        runtime.close()
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
