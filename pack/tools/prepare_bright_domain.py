#!/usr/bin/env python3
"""Build Bright-Pro domain CSV + unique stripped embedding shards.

Reads yale-nlp/Bright-Pro documents parquet (id, content) and writes:
  - articles.csv with columns id,body (body = content after strip for storage
    consistency with Quail CSV import, which also strips)
  - shards/shard-XX.jsonl with unique {text_hash, body} where text_hash is
    SHA-256 of the stripped body (must match quail.search.vectors.text_hash)

Usage:
  prepare_bright_domain.py --parquet documents/biology.parquet \\
      --csv data/articles.csv --shards-dir pack/biology/shards --n-shards 10
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path

import pyarrow.parquet as pq


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--parquet", type=Path, required=True)
    p.add_argument("--csv", type=Path, required=True)
    p.add_argument("--shards-dir", type=Path, required=True)
    p.add_argument("--n-shards", type=int, default=10)
    args = p.parse_args()
    if args.n_shards < 1:
        raise SystemExit("--n-shards must be >= 1")

    table = pq.read_table(args.parquet)
    if set(table.column_names) < {"id", "content"}:
        raise SystemExit(f"unexpected columns: {table.column_names}")

    ids = table["id"].to_pylist()
    contents = table["content"].to_pylist()
    if len(ids) != len(contents):
        raise SystemExit("id/content length mismatch")

    args.csv.parent.mkdir(parents=True, exist_ok=True)
    # Quail CSV import strips cell values; store stripped bodies so file == DB text.
    with args.csv.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["id", "body"], quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        unique: dict[str, str] = {}
        empty = 0
        for doc_id, content in zip(ids, contents, strict=True):
            if not isinstance(doc_id, str) or not isinstance(content, str):
                raise SystemExit(f"bad types id={type(doc_id)} content={type(content)}")
            body = content.strip()
            if not body:
                empty += 1
                # Still import empty? Quail may reject; keep row with empty body.
            w.writerow({"id": doc_id, "body": body})
            digest = text_hash(body)
            # First wins; duplicates share the same vector at warm time.
            unique.setdefault(digest, body)

    print(
        f"csv={args.csv} rows={len(ids)} unique_hashes={len(unique)} empty_bodies={empty}",
        flush=True,
    )

    items = sorted(unique.items(), key=lambda kv: kv[0])
    args.shards_dir.mkdir(parents=True, exist_ok=True)
    # Clear old shards
    for old in args.shards_dir.glob("shard-*.jsonl"):
        old.unlink()

    buckets: list[list[tuple[str, str]]] = [[] for _ in range(args.n_shards)]
    for i, (digest, body) in enumerate(items):
        buckets[i % args.n_shards].append((digest, body))

    for i, bucket in enumerate(buckets):
        path = args.shards_dir / f"shard-{i:02d}.jsonl"
        with path.open("w", encoding="utf-8") as out:
            for digest, body in bucket:
                out.write(json.dumps({"text_hash": digest, "body": body}, ensure_ascii=False) + "\n")
        print(f"wrote {path} n={len(bucket)}", flush=True)


if __name__ == "__main__":
    main()
