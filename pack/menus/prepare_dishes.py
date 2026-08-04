#!/usr/bin/env python3
"""Build dishes.csv (+ optional shards) for the NYPL dish pack.

Reads examples/nypl-menus/data/nypl_items.csv (from prepare.py there) and writes:
  - data/dishes.csv — item rows with Quail-required `id` + stripped `body`
  - pack/menus/shards/shard-XX.jsonl — unique {text_hash, body} (unless --skip-shards)

text_hash = SHA-256 of stripped UTF-8 body (matches Quail CSV import + vectors).

Usage (from repo root, after examples/nypl-menus/prepare.py has run):
  python pack/menus/prepare_dishes.py
  python pack/menus/prepare_dishes.py --skip-shards   # CSV only; keep existing shards
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path

csv.field_size_limit(10_000_000)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ITEMS = ROOT / "examples" / "nypl-menus" / "data" / "nypl_items.csv"
DEFAULT_CSV = ROOT / "data" / "dishes.csv"
DEFAULT_SHARDS = Path(__file__).resolve().parent / "shards"

# Quail requires unique `id`. Slim column set keeps CSV import memory workable
# for ~1.33M rows (page coords / sparse catalog fields stay in nypl_items).
OUT_FIELDS = [
    "id",
    "body",
    "price",
    "high_price",
    "menu_id",
    "date",
    "year",
    "location",
    "event",
    "venue",
    "place",
    "currency",
]


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--items", type=Path, default=DEFAULT_ITEMS)
    p.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    p.add_argument("--shards-dir", type=Path, default=DEFAULT_SHARDS)
    p.add_argument("--n-shards", type=int, default=20)
    p.add_argument(
        "--skip-shards",
        action="store_true",
        help="only rebuild dishes.csv; do not rewrite pack/menus/shards",
    )
    args = p.parse_args()
    if args.n_shards < 1:
        raise SystemExit("--n-shards must be >= 1")
    if not args.items.exists():
        raise SystemExit(
            f"missing {args.items}; run examples/nypl-menus/prepare.py first"
        )

    args.csv.parent.mkdir(parents=True, exist_ok=True)
    unique: dict[str, str] = {}
    rows = 0
    empty = 0
    with args.items.open(newline="", encoding="utf-8") as src, args.csv.open(
        "w", newline="", encoding="utf-8"
    ) as dst:
        reader = csv.DictReader(src)
        writer = csv.DictWriter(dst, fieldnames=OUT_FIELDS, quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        for row in reader:
            body = (row.get("dish_name") or "").strip()
            if not body:
                empty += 1
            out = {
                "id": row.get("item_id", ""),
                "body": body,
                "price": row.get("price", ""),
                "high_price": row.get("high_price", ""),
                "menu_id": row.get("menu_id", ""),
                "date": row.get("date", ""),
                "year": row.get("year", ""),
                "location": row.get("location", ""),
                "event": row.get("event", ""),
                "venue": row.get("venue", ""),
                "place": row.get("place", ""),
                "currency": row.get("currency", ""),
            }
            writer.writerow(out)
            rows += 1
            if body:
                unique.setdefault(text_hash(body), body)

    print(
        f"csv={args.csv} rows={rows} unique_hashes={len(unique)} empty_bodies={empty}",
        flush=True,
    )

    if args.skip_shards:
        print("skip-shards: left pack/menus/shards untouched", flush=True)
        return

    items = sorted(unique.items(), key=lambda kv: kv[0])
    args.shards_dir.mkdir(parents=True, exist_ok=True)
    for old in args.shards_dir.glob("shard-*.jsonl"):
        old.unlink()

    buckets: list[list[tuple[str, str]]] = [[] for _ in range(args.n_shards)]
    for i, (digest, body) in enumerate(items):
        buckets[i % args.n_shards].append((digest, body))

    for i, bucket in enumerate(buckets):
        path = args.shards_dir / f"shard-{i:02d}.jsonl"
        with path.open("w", encoding="utf-8") as out:
            for digest, body in bucket:
                out.write(
                    json.dumps(
                        {"text_hash": digest, "body": body}, ensure_ascii=False
                    )
                    + "\n"
                )
        print(f"wrote {path} n={len(bucket)}", flush=True)

    bad = 0
    for path in sorted(args.shards_dir.glob("shard-*.jsonl")):
        with path.open(encoding="utf-8") as f:
            for line in f:
                row = json.loads(line)
                if text_hash(row["body"]) != row["text_hash"]:
                    bad += 1
                if row["body"] != row["body"].strip():
                    bad += 1
    if bad:
        raise SystemExit(f"shard integrity failed: {bad} bad rows")
    print("shard integrity ok", flush=True)


if __name__ == "__main__":
    main()
