Vector shards land here as `shard-XX.jsonl.gz` from embed workers.

Each line: `{"text_hash": "<sha256>", "vector": [<768 floats>]}`

After all 20 land on the branch, the parent joins them for warm (do not invent warm logic in workers).
