# Psychology pack + ChatGPT Agent — operational handoff

Last updated: 2026-08-03. Write this down so context compaction does not erase how we got here.

Audience: future Cursor cloud agents continuing this eval path. Prefer reading this before inventing a new upload or embed strategy.

---

## Product shape

Blind-ish eval of **base Quail** (later a RAG condition) on Bright-Pro-style domains.

| Piece | Choice |
|-------|--------|
| Eval surface | ChatGPT **chat** Agent (chat rate limits) |
| Automation | Cursor computer-use / Codex as thin harness only |
| Pack count | **domains × conditions** (discussed full matrix **7 × 2 = 14**), not × queries |
| Queries | ~175 = 25 × 7 agentic sample; **one question per chat session** |
| Branch | `psychology` (dataset-named on purpose; do not rename to `cursor/…` for this pack) |
| Merge to `main` | **No** for this draft work unless the human says otherwise |

ChatGPT chat Agent is the eval meter we care about. Codex/work has better tooling but the wrong rate limit. Computer-use is for library upload, opening chats, attaching files, pasting prompts — not for answering the psych questions.

---

## Architecture in one picture

```text
Cursor cloud (has egress, Ollama, Quail)
  ├─ shard unique bodies → N subagents embed via local Ollama
  ├─ parent PrecomputedEmbedder + process_config → warmed Turso DBs
  ├─ zip pack (+ later ollama/model zips)
  ├─ optional R2 mirror for CU download
  └─ computer-use uploads zips → ChatGPT Library

ChatGPT chat Agent VM (almost no egress)
  ├─ attach zips from Library (only proven large-binary path)
  ├─ unzip + assemble.sh → ASSEMBLE_OK
  ├─ (next) unzip ollama runtime + model store → ollama serve
  └─ quail run / MCP → answer ONE question → end chat
```

---

## A. How psychology embeds were built (Cursor-side)

This is the pattern that worked. Reuse for other domains.

### Identity of the pack

`quail.toml` embedding profile (must stay stable for this DB):

```toml
[datasets.embedding]
provider = "ollama"
model = "embeddinggemma:300m-qat-q8_0"
dimensions = 768
revision = "embeddinggemma-300m-qat-q8-v1"
fields = ["body"]
```

`profile_hash` includes **provider**, model, dims, revision, fields. Switching the TOML to OpenRouter (or any other provider) against this warmed DB will **not** cleanly sync and can fail warm/pin. Query embeds for this pack must stay Ollama-compatible with the same model id / dims / revision.

Warm receipt (approx):

- ~54,741 entries
- ~45,660 unique embedding texts
- `lexical_ready` + `embedding_ready`
- Search assemble checksum: `9e4f0683476f21224b227f79677b785f073ac73a35b530a478fc9e893b34c7e6`

### Critical hash/strip lesson

1. CSV import **strips** cell text before Quail hashes for warm.
2. First 10-way shard run hashed **unstripped** body → mass `text_hash` mismatch.
3. Fix: regenerate shards with `body.strip()` for `text_hash`; remap ~42k vectors; locally re-embed ~3.3k whitespace-only edge cases.
4. **Always strip before `text_hash` / shard write.** Assert every warm `text_hash` exists in the vector map before `process_config`.

### Parallel embed pattern (subagents)

Parent agent (smart):

1. Build unique stripped bodies → `text_hash` → shard JSONL under `pack/psychology/shards/`.
2. Push branch so cloud workers can see shards.
3. Launch **N cloud subagents** (we used **10**) as **dumb Ollama workers**.
4. Each worker runs `pack/psychology/embed_worker.py`:
   - reads shard rows `{text_hash, body}`
   - POSTs `http://127.0.0.1:11434/api/embed` with model + dimensions
   - writes gzip jsonl `{text_hash, vector}`
5. Parent collects vectors → in-memory / on-disk map keyed by `text_hash`.
6. Parent runs Quail `process_config` with a **`PrecomputedEmbedder`** (factory override) so process never calls live embed for corpus texts.
7. Quail has **no** true “lexical then embeddings-only” partial process; offline vectors + one full process is the approach.

Worker defaults match the pack: model `embeddinggemma:300m-qat-q8_0`, dims `768`, batch 32, retries with backoff.

What “dumb worker” means: do **not** ask subagents to invent process logic. Give them shard path, out path, and the embed_worker script. Parent owns remapping, missing-hash repair, and `process_config`.

### Shipping the DBs (no Git LFS)

Git LFS was abandoned for ChatGPT / normal git:

- `data/quail.turso` — single file (~95MB)
- `data/quail-search.turso.part00–04` — ~80MiB chunks + `data/quail-search.turso.sha256`
- `data/articles.csv`
- Repo assemble: `pack/psychology/assemble_data.sh`
- ChatGPT zip assemble: `/tmp/psychology-base-quail/assemble.sh` (prints `ASSEMBLE_OK`)

CSV field-size limit was raised in `quail/datasets/csv_import/` (+ twin `.txt`) for this corpus.

---

## B. ChatGPT Agent VM constraints (learned the hard way)

The Agent VM is **not** a normal Linux box with internet.

| Action | Result |
|--------|--------|
| `git clone` | No — no general egress |
| `apt` / `ollama pull` / curl-install from internet | No — blocked egress |
| `pip` / `npm` | Often yes — via OpenAI package proxy |
| `container.download` of `text/plain` | Works |
| `container.download` of small non-special `octet-stream` | Works |
| `container.download` of zip / gzip | **Blocked** (content sniff / type deny) |
| `container.download` of `.sh` (shellscript) | **Blocked** |
| `container.download` of sqlite | **Blocked** (`application/vnd.sqlite3`) |
| **Upload zip → ChatGPT Library → attach in chat** | **Works**, including real SQLite inside zip |

Implication: anything large or “special” (zip, sqlite, model blobs, ollama binary) must enter via **Library upload**, not `container.download`.

Library vs Project: upload to **Library**, then attach from library into a new chat. That is the proven path.

Upload size: keep individual zips under ~**512MB** ChatGPT upload cap. Split if needed.

---

## C. Proven smoke (2026-08-03)

1. Built `/tmp/psychology-base-quail.zip` (~249MB / ~250M on disk):
   - `quail.toml`, `WORKSPACE.md`, `PACK.txt`
   - `assemble.sh`
   - `data/articles.csv`, `data/quail.turso`, search parts + sha256
2. Mirrored to R2: `https://pub-cc081ad11c2848bea7efb624147a8ae4.r2.dev/packs/psychology-base-quail.zip`
3. Computer-use downloaded/uploaded that zip into ChatGPT **Library** as `psychology-base-quail.zip`
4. New chat → attach from library → unzip under `/mnt/data/` → `bash assemble.sh` → **`ASSEMBLE_OK`**
5. Example chat: `https://chatgpt.com/c/6a7097b9-73bc-83eb-9af3-beafd3402082`

Assemble smoke prompt that worked:

```text
Unzip psychology-base-quail.zip into /mnt/data/, then:
  cd /mnt/data/psychology-base-quail && bash assemble.sh
Report the full ASSEMBLE_OK output.
```

That smoke did **not** need EmbeddingGemma or Ollama. It only proved: library zip → unzip → assemble DBs.

---

## D. Computer-use session notes (Cursor)

- Main Cursor agent has **no** direct browser. GUI work is `Task` + `subagent_type=computerUse`.
- **Sticky CU:** first computer-use subagent in a run keeps its model + browser cookies. Later `model:` overrides do **not** unstick it. This run’s CU is Sonnet; human OK keeping it because ChatGPT login already succeeded.
- Fresh Grok CU requires a **new cloud agent run** (do not fight sticky Sonnet mid-run).
- Google login + phone 2FA needed once; after that, library upload automation works.
- **Do not** commit passwords, R2 secret keys, or Google credentials into the repo. If secrets were pasted in chat, rotate them.
- Email used for ChatGPT Google login in this work: `dacoyier@gmail.com` (identity only; not a secret).

CU’s job for eval:

1. Ensure pack (+ later ollama/model) zips are in Library
2. Open a fresh chat
3. Attach needed library files
4. Paste a tight prompt (assemble / start ollama / quail / answer one question)
5. Capture the answer; leave scoring to humans or a later harness

---

## E. Cloudflare R2 (Cursor ↔ human/CU bridge)

| Item | Value |
|------|-------|
| Bucket | `quail` |
| Public base | `https://pub-cc081ad11c2848bea7efb624147a8ae4.r2.dev` |
| Account id (non-secret) | `b24d37c947a6326003c797623c27b8bb` |
| Pack object | `packs/psychology-base-quail.zip` |
| Helper | `pack/psychology/upload_smoke_to_r2.sh` |

R2 free tier: ~10GB storage, free egress — fine for many **links**; storage binds if we ship ~14 full domain packs plus models.

**R2 is not a substitute for Library inside the Agent VM.** Agent cannot `container.download` the zip. R2 is for Cursor → CU download → Library upload.

Old tiny objects under `quail-smoke/` were deleted after the real pack landed.

---

## F. Next step: Ollama + EmbeddingGemma as Library zips

Yes — these should be **new uploads**, same pattern as the DB pack. Do not try to pull models inside ChatGPT.

### Why

This pack’s `profile_hash` is ollama + q8 EmbeddingGemma. Query-time embeds must hit a compatible Ollama `/api/embed`. The Agent VM cannot `ollama pull`.

### Proposed artifacts (each under ~512MB)

On this Cursor VM today:

| Asset | Notes | Approx size |
|-------|--------|-------------|
| `/usr/local/bin/ollama` | Linux x86_64, v0.32.5 | ~38MB |
| q8 model blob `sha256-ed705a9a…` | main weights | ~323MB |
| q8 license + params + config blobs | tiny | ~12KB |
| q8 manifest | `…/embeddinggemma/300m-qat-q8_0` | tiny |
| Full `embeddinggemma:300m` (non-q8) | **Do not ship** for this pack | ~594MB extra |

Proposed zip split:

1. **`ollama-runtime.zip`** (~40MB)
   - `bin/ollama` (x86_64)
   - `run_ollama.sh` — set `OLLAMA_HOST=127.0.0.1:11434`, `OLLAMA_MODELS=…`, start serve in background, wait until `/api/tags` responds
2. **`embeddinggemma-q8-model.zip`** (~330MB)
   - Minimal Ollama models tree for **only** `embeddinggemma:300m-qat-q8_0`
   - Layout Ollama expects under `$OLLAMA_MODELS` (usually `blobs/` + `manifests/registry.ollama.ai/library/embeddinggemma/300m-qat-q8_0`)
   - Exclude the larger non-q8 `300m` blobs

Optional: also mirror both on R2 under `packs/` for CU to fetch before Library upload.

### Agent session recipe (target)

Library attachments for a full base-Quail psych trial:

1. `psychology-base-quail.zip` (already uploaded)
2. `ollama-runtime.zip` (new)
3. `embeddinggemma-q8-model.zip` (new)
4. Quail install path TBD — wheel via pip proxy, or another small library zip

Prompt sketch:

```text
1) Unzip psychology-base-quail.zip → /mnt/data/psychology-base-quail && bash assemble.sh
2) Unzip ollama-runtime.zip + embeddinggemma-q8-model.zip → /mnt/data/ollama-bundle
3) Start ollama via run_ollama.sh (models dir = bundled store)
4) Confirm: curl -s http://127.0.0.1:11434/api/tags shows embeddinggemma:300m-qat-q8_0
5) Install/start Quail against /mnt/data/psychology-base-quail/quail.toml
6) Answer ONLY this question: <ONE QUESTION>
7) Do not browse the open web for the answer; use Quail retrieval.
```

Still TBD: how Quail binary/wheel lands in the VM (pip from OpenAI proxy vs uploaded sdist/wheel zip). Prefer testing pip first once Ollama is up.

### What not to do

- Do not change `provider` to OpenRouter for this warmed pack.
- Do not `ollama pull` inside ChatGPT.
- Do not ship one mega-zip that blows the upload cap.
- Do not put one zip per question; reuse Library attaches across one-question chats.

---

## G. File map

| Artifact | Location |
|----------|----------|
| Repo branch | `psychology` |
| Operator config | `quail.toml` |
| Embed worker | `pack/psychology/embed_worker.py` |
| Shards | `pack/psychology/shards/shard-00.jsonl` … |
| Repo assemble | `pack/psychology/assemble_data.sh` |
| R2 upload helper | `pack/psychology/upload_smoke_to_r2.sh` |
| This handoff | `pack/psychology/OPS_HANDOFF.md` |
| Built ChatGPT pack dir | `/tmp/psychology-base-quail/` |
| Built ChatGPT pack zip | `/tmp/psychology-base-quail.zip` |
| R2 URL | `https://pub-cc081ad11c2848bea7efb624147a8ae4.r2.dev/packs/psychology-base-quail.zip` |
| ChatGPT library name | `psychology-base-quail.zip` |
| Local Ollama | `/usr/local/bin/ollama` + `~/.ollama/models` |
| Local q8 model | `embeddinggemma:300m-qat-q8_0` (id `e84a7acc2394`, ~338MB listed) |

---

## H. Pitfalls checklist

1. **Strip before hash** or vectors will not match warm.
2. **Provider is part of profile_hash** — keep ollama for this pack.
3. **Library upload, not container.download**, for zips/sqlite/models.
4. **CU model is sticky** — don’t waste turns trying to switch Sonnet→Grok mid-run.
5. **Phone download of 250MB+** is painful; prefer CU library upload from this VM.
6. **One question per chat**; many chats reuse the same library files.
7. **Do not merge** this psychology draft to `main` unless asked.
8. **Secrets** that appeared in chat history should be rotated; never re-commit them into notes.

---

## I. Unrestricted MCP smoke (2026-08-03, current)

In-process `smoke_semantic.py` only proves `exec_script`. Production-shaped path is:

1. `quail run --config <pack>/quail.toml` → streamable-http MCP at `http://127.0.0.1:8000/mcp`
2. MCP client: `quail_setup` → `quail_exec` with `Semantic(...)` / `Lexical(...)`

Shipped helpers (also inside `quail-wheel.zip`):

| Script | Role |
|--------|------|
| `smoke_all.sh` | assemble → install wheel → start Ollama → embed API → in-process smoke → **start `quail run`** → MCP smoke |
| `smoke_semantic.py` | host `exec_script` Lexical/Semantic (narrow) |
| `smoke_mcp.py` | MCP SDK `streamable_http_client` → setup + exec |

Proven locally on this VM against mini pack: `SMOKE_MCP_OK` with cognitive-dissonance hits via `quail_exec`.

### Attach budget

~512MB **per message**, not Library storage. Full pack (~250) + model (~302) exceeds one message; **split across messages in the same chat** works. Mini pack (~13MB, 800 rows) is pipeline smoke only — not eval quality.

### Agent smoke prompt (MCP)

Attach from Library (split messages if needed): `psychology-mini-quail.zip` or `psychology-base-quail.zip`, `ollama-runtime.zip`, `embeddinggemma-q8-model.zip`, `quail-wheel.zip`.

```text
Unzip all attached zips under /mnt/data/ (tolerate name(N)/ folders).
Run:
  bash /mnt/data/quail-wheel*/smoke_all.sh /mnt/data
  # if nested: bash /mnt/data/quail-wheel*/quail-wheel/smoke_all.sh /mnt/data
Report lines containing: ASSEMBLE_OK, EMBED_API_OK, SMOKE_SEMANTIC_OK, MCP_SETUP_OK, SMOKE_MCP_OK, SMOKE_ALL_OK.
Leave quail run up at http://127.0.0.1:8000/mcp.
```

### Eval prompt shape (after smoke)

Server already running. One question per chat:

```text
Quail MCP is at http://127.0.0.1:8000/mcp (unrestricted local).
Use MCP tools: quail_setup once, then quail_exec with Semantic/Lexical retrieve
against dataset articles. Answer ONLY this question using Quail retrieval
(print evidence ids + short quotes). Do not browse the open web.
QUESTION: <ONE QUESTION>
```

Whether ChatGPT natively attaches to localhost MCP is still **unproven**; `smoke_mcp.py` proves the server. If native connect fails, the Agent can drive tools via `python3 smoke_mcp.py`-style calls or inline MCP client code.

### R2 mirrors (CU → Library)

| Object | URL |
|--------|-----|
| psychology-base-quail.zip | `https://pub-cc081ad11c2848bea7efb624147a8ae4.r2.dev/packs/psychology-base-quail.zip` |
| psychology-mini-quail.zip | `https://pub-cc081ad11c2848bea7efb624147a8ae4.r2.dev/packs/psychology-mini-quail.zip` |
| ollama-runtime.zip | `https://pub-cc081ad11c2848bea7efb624147a8ae4.r2.dev/packs/ollama-runtime.zip` |
| embeddinggemma-q8-model.zip | `https://pub-cc081ad11c2848bea7efb624147a8ae4.r2.dev/packs/embeddinggemma-q8-model.zip` |
| quail-wheel.zip | `https://pub-cc081ad11c2848bea7efb624147a8ae4.r2.dev/packs/quail-wheel.zip` |

---

## J. Suggested next actions (in order)

1. Re-upload refreshed `quail-wheel.zip` (with `smoke_mcp.py`) to ChatGPT Library.
2. Agent chat: run `smoke_all.sh` → confirm `SMOKE_MCP_OK`.
3. Same chat or follow-up: one-question eval via MCP tools (native or scripted).
4. Switch attach to **full** `psychology-base-quail.zip` (multi-message) for production-quality retrieval.
5. Later: other domains; RAG condition packs; harness that opens one chat per question.

---

## K. Alternative path (parked)

Hosted Quail MCP behind a tunnel so the Agent never needs DBs/models in-VM. Still valid, but the current bet is **fully local Agent VM** via Library zips so the eval stays closer to “agent with tools on a sealed machine.”
