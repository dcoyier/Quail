#!/usr/bin/env python3
"""Build the NYPL menu-document CSV and unique-body embedding shards."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path

csv.field_size_limit(10_000_000)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE = (
    ROOT / "examples" / "nypl-menus" / "data" / "nypl_menu_docs.csv"
)
DEFAULT_CSV = ROOT / "data" / "menu_docs.csv"
DEFAULT_SHARDS = Path(__file__).resolve().parent / "shards"

OUT_FIELDS = [
    "id",
    "body",
    "date",
    "year",
    "location",
    "sponsor",
    "event",
    "venue",
    "place",
    "occasion",
    "currency",
    "page_count",
    "dish_count",
]


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--shards-dir", type=Path, default=DEFAULT_SHARDS)
    parser.add_argument("--n-shards", type=int, default=20)
    parser.add_argument(
        "--skip-shards",
        action="store_true",
        help="only rebuild menu_docs.csv; do not rewrite embedding shards",
    )
    args = parser.parse_args()
    if args.n_shards < 1:
        raise SystemExit("--n-shards must be >= 1")
    if not args.source.exists():
        raise SystemExit(
            f"missing {args.source}; run examples/nypl-menus/prepare.py first"
        )

    args.csv.parent.mkdir(parents=True, exist_ok=True)
    unique: dict[str, str] = {}
    rows = 0
    empty = 0
    with args.source.open(newline="", encoding="utf-8") as source, args.csv.open(
        "w", newline="", encoding="utf-8"
    ) as destination:
        reader = csv.DictReader(source)
        writer = csv.DictWriter(
            destination,
            fieldnames=OUT_FIELDS,
            quoting=csv.QUOTE_MINIMAL,
        )
        writer.writeheader()
        for row in reader:
            body = (row.get("menu_text") or "").strip()
            if not body:
                empty += 1
            output = {
                "id": row.get("menu_id", ""),
                "body": body,
                "date": row.get("date", ""),
                "year": row.get("year", ""),
                "location": row.get("location", ""),
                "sponsor": row.get("sponsor", ""),
                "event": row.get("event", ""),
                "venue": row.get("venue", ""),
                "place": row.get("place", ""),
                "occasion": row.get("occasion", ""),
                "currency": row.get("currency", ""),
                "page_count": row.get("page_count", ""),
                "dish_count": row.get("dish_count", ""),
            }
            writer.writerow(output)
            rows += 1
            if body:
                unique.setdefault(text_hash(body), body)

    print(
        f"csv={args.csv} rows={rows} unique_hashes={len(unique)} "
        f"empty_bodies={empty}",
        flush=True,
    )
    if args.skip_shards:
        print("skip-shards: left menu-document shards untouched", flush=True)
        return

    items = sorted(unique.items(), key=lambda item: item[0])
    args.shards_dir.mkdir(parents=True, exist_ok=True)
    for old in args.shards_dir.glob("shard-*.jsonl"):
        old.unlink()

    buckets: list[list[tuple[str, str]]] = [
        [] for _ in range(args.n_shards)
    ]
    for index, item in enumerate(items):
        buckets[index % args.n_shards].append(item)

    for index, bucket in enumerate(buckets):
        path = args.shards_dir / f"shard-{index:02d}.jsonl"
        with path.open("w", encoding="utf-8") as output:
            for digest, body in bucket:
                output.write(
                    json.dumps(
                        {"text_hash": digest, "body": body},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        print(f"wrote {path} n={len(bucket)}", flush=True)

    bad = 0
    for path in sorted(args.shards_dir.glob("shard-*.jsonl")):
        with path.open(encoding="utf-8") as source:
            for line in source:
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
