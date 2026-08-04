#!/usr/bin/env python3
"""Embed a shard with Ollama EmbeddingGemma; write jsonl.gz of {text_hash, vector}."""
from __future__ import annotations

import argparse
import gzip
import json
import time
import urllib.request
from pathlib import Path


def embed_batch(
    base_url: str, model: str, dimensions: int, texts: list[str], timeout: int = 600
) -> list[list[float]]:
    url = f"{base_url.rstrip('/')}/api/embed"
    payload = json.dumps(
        {"model": model, "input": texts, "dimensions": dimensions}
    ).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read())
    vectors = body.get("embeddings")
    if not isinstance(vectors, list) or len(vectors) != len(texts):
        raise RuntimeError(f"bad embeddings response size: {type(vectors)}")
    return vectors


def ensure_ollama(model: str) -> None:
    """Best-effort: start serve if needed and pull model. Workers may already have it."""
    import shutil
    import subprocess

    if shutil.which("ollama") is None:
        raise SystemExit("ollama not on PATH; install before running this worker")
    try:
        urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=2).read()
    except Exception:
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(60):
            try:
                urllib.request.urlopen(
                    "http://127.0.0.1:11434/api/tags", timeout=2
                ).read()
                break
            except Exception:
                time.sleep(0.5)
        else:
            raise SystemExit("ollama serve did not become ready")
    tags = json.loads(
        urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=30).read()
    )
    names = [m.get("name", "") for m in tags.get("models") or []]
    if not any(model in n for n in names):
        print(f"pulling {model} ...", flush=True)
        subprocess.check_call(["ollama", "pull", model])


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--shard", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--base-url", default="http://127.0.0.1:11434")
    p.add_argument("--model", default="embeddinggemma:300m-qat-q8_0")
    p.add_argument("--dimensions", type=int, default=768)
    p.add_argument("--batch-size", type=int, default=32)
    p.add_argument("--ensure-ollama", action="store_true")
    args = p.parse_args()

    if args.ensure_ollama:
        ensure_ollama(args.model)

    rows = []
    with args.shard.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    print(f"loaded {len(rows)} from {args.shard}", flush=True)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    done = 0
    t0 = time.perf_counter()
    with gzip.open(args.out, "wt", encoding="utf-8") as out:
        for i in range(0, len(rows), args.batch_size):
            batch = rows[i : i + args.batch_size]
            texts = [r["body"] for r in batch]
            for attempt in range(5):
                try:
                    vectors = embed_batch(
                        args.base_url, args.model, args.dimensions, texts
                    )
                    break
                except Exception as e:
                    wait = 2**attempt
                    print(
                        f"batch {i} attempt {attempt+1} failed: {e}; sleep {wait}s",
                        flush=True,
                    )
                    time.sleep(wait)
            else:
                raise RuntimeError(f"batch starting at {i} failed permanently")
            for row, vec in zip(batch, vectors, strict=True):
                if len(vec) != args.dimensions:
                    raise RuntimeError(f"dim {len(vec)} != {args.dimensions}")
                out.write(
                    json.dumps({"text_hash": row["text_hash"], "vector": vec}) + "\n"
                )
            done += len(batch)
            elapsed = time.perf_counter() - t0
            rate = done / elapsed if elapsed else 0
            print(
                f"progress {done}/{len(rows)}  {rate:.1f}/s  elapsed={elapsed:.0f}s",
                flush=True,
            )
    print(
        f"wrote {args.out}  total={done}  wall={time.perf_counter()-t0:.1f}s",
        flush=True,
    )


if __name__ == "__main__":
    main()
