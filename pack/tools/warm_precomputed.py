#!/usr/bin/env python3
"""Warm a domain pack using precomputed EmbeddingGemma shard vectors."""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path

from quail.config.parse import load_config
from quail.run.process import process_config
from quail.search.vectors import text_hash


class PrecomputedEmbedder:
    def __init__(self, vectors: dict[str, list[float]], dimensions: int) -> None:
        self._vectors = vectors
        self._dimensions = dimensions
        self._missing = 0

    def embed_texts(self, texts):
        out: list[list[float]] = []
        for text in texts:
            digest = text_hash(text)
            vec = self._vectors.get(digest)
            if vec is None:
                self._missing += 1
                raise KeyError(f"missing precomputed vector for text_hash={digest}")
            if len(vec) != self._dimensions:
                raise ValueError(f"dim {len(vec)} != {self._dimensions} for {digest}")
            out.append(vec)
        return out


def load_vectors(paths: list[Path]) -> dict[str, list[float]]:
    vectors: dict[str, list[float]] = {}
    for path in paths:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                vectors[row["text_hash"]] = row["vector"]
        print(f"loaded {path} -> map_size={len(vectors)}", flush=True)
    return vectors


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--vectors-dir", type=Path, required=True)
    p.add_argument("--clear", action="store_true")
    args = p.parse_args()

    paths = sorted(args.vectors_dir.glob("shard-*.jsonl.gz"))
    if not paths:
        raise SystemExit(f"no shard-*.jsonl.gz under {args.vectors_dir}")
    vectors = load_vectors(paths)
    print(f"total unique vectors={len(vectors)}", flush=True)

    config = load_config(args.config)
    dims = None
    for spec in config.datasets:
        if spec.embedding is not None:
            dims = spec.embedding.dimensions
            break
    if dims is None:
        raise SystemExit("no dataset embedding profile in config")

    embedder = PrecomputedEmbedder(vectors, dims)

    def factory(profile):
        if profile.dimensions != dims:
            raise RuntimeError("profile dims mismatch")
        return embedder

    outcome = process_config(config, clear=args.clear, embedder_factory=factory)
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


if __name__ == "__main__":
    main()
