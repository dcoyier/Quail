#!/usr/bin/env python3
"""Warm a domain pack using precomputed EmbeddingGemma shard vectors.

Vectors from one or more shard directories are loaded into an on-disk SQLite
cache first so large packs (100k+ unique texts) do not keep float maps in RAM
during CSV import + warm.
"""
from __future__ import annotations

import argparse
import gzip
import json
import sqlite3
import struct
import threading
from pathlib import Path

from quail.config.parse import load_config
from quail.run.process import process_config
from quail.search.vectors import text_hash


class PrecomputedEmbedder:
    """Look up corpus vectors by Quail text_hash (SHA-256 of stripped UTF-8)."""

    def __init__(self, conn: sqlite3.Connection, dimensions: int) -> None:
        self._conn = conn
        self._dimensions = dimensions
        self._missing = 0
        self._pack = struct.Struct(f"<{dimensions}f")
        self._lock = threading.Lock()

    def embed_texts(self, texts):
        digests = [text_hash(text) for text in texts]
        placeholders = ",".join("?" for _ in digests)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT text_hash, vector FROM vectors WHERE text_hash IN ({placeholders})",
                digests,
            ).fetchall()
        by_digest = {str(row[0]): row[1] for row in rows}
        out: list[list[float]] = []
        for digest in digests:
            blob = by_digest.get(digest)
            if blob is None:
                self._missing += 1
                raise KeyError(f"missing precomputed vector for text_hash={digest}")
            if len(blob) != self._pack.size:
                raise ValueError(
                    f"blob bytes {len(blob)} != {self._pack.size} for {digest}"
                )
            out.append(list(self._pack.unpack(blob)))
        return out


def build_vector_cache(paths: list[Path], cache_path: Path, dimensions: int) -> int:
    """Materialize shard gzips into SQLite (text_hash -> float32 blob)."""
    if cache_path.exists():
        cache_path.unlink()
    conn = sqlite3.connect(cache_path)
    try:
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute(
            "CREATE TABLE vectors (text_hash TEXT PRIMARY KEY, vector BLOB NOT NULL)"
        )
        pack = struct.Struct(f"<{dimensions}f")
        for path in paths:
            batch: list[tuple[str, bytes]] = []
            with gzip.open(path, "rt", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    row = json.loads(line)
                    vec = row["vector"]
                    if len(vec) != dimensions:
                        raise SystemExit(
                            f"{path}: dim {len(vec)} != {dimensions} for {row['text_hash']}"
                        )
                    batch.append((row["text_hash"], pack.pack(*vec)))
                    if len(batch) >= 5000:
                        conn.executemany(
                            "INSERT OR REPLACE INTO vectors(text_hash, vector) VALUES (?, ?)",
                            batch,
                        )
                        batch.clear()
            if batch:
                conn.executemany(
                    "INSERT OR REPLACE INTO vectors(text_hash, vector) VALUES (?, ?)",
                    batch,
                )
            count = conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
            print(f"loaded {path} -> cache_size={count}", flush=True)
        conn.commit()
        return int(conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0])
    finally:
        conn.close()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--config", type=Path, required=True)
    p.add_argument(
        "--vectors-dir",
        dest="vector_dirs",
        type=Path,
        action="append",
        required=True,
        help="Shard directory; repeat to warm a config containing multiple corpora",
    )
    p.add_argument(
        "--cache",
        type=Path,
        default=None,
        help="SQLite path for vector cache (default: <vectors-dir>/_vector_cache.sqlite)",
    )
    p.add_argument("--reuse-cache", action="store_true")
    p.add_argument("--clear", action="store_true")
    args = p.parse_args()

    paths: list[Path] = []
    for vector_dir in args.vector_dirs:
        directory_paths = sorted(vector_dir.glob("shard-*.jsonl.gz"))
        if not directory_paths:
            raise SystemExit(f"no shard-*.jsonl.gz under {vector_dir}")
        paths.extend(directory_paths)

    config = load_config(args.config)
    dims = None
    for spec in config.datasets:
        if spec.embedding is not None:
            dims = spec.embedding.dimensions
            break
    if dims is None:
        raise SystemExit("no dataset embedding profile in config")

    cache_path = args.cache or (args.vector_dirs[0] / "_vector_cache.sqlite")
    if args.reuse_cache and cache_path.exists():
        print(f"reusing vector cache {cache_path}", flush=True)
    else:
        n = build_vector_cache(paths, cache_path, dims)
        print(f"total unique vectors={n}", flush=True)

    conn = sqlite3.connect(
        f"file:{cache_path.as_posix()}?mode=ro",
        uri=True,
        check_same_thread=False,
    )
    embedder = PrecomputedEmbedder(conn, dims)

    def factory(profile):
        if profile.dimensions != dims:
            raise RuntimeError("profile dims mismatch")
        return embedder

    try:
        outcome = process_config(
            config,
            clear=args.clear,
            embedder_factory=factory,
            progress=lambda message: print(message, flush=True),
        )
        for r in outcome.results:
            print(
                f"warmed {r.dataset_id} texts={r.text_count} unique={r.unique_text_count} "
                f"batches={r.embedded_batches} lexical={r.lexical_ready} emb={r.embedding_ready} "
                f"fp={r.build_fingerprint}",
                flush=True,
            )
        if embedder._missing:
            raise SystemExit(f"missing lookups={embedder._missing}")
        print("WARM_OK", flush=True)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
