# Senior-Level Elevation Plan — Dutch Tax Authority RAG

> **Assessment feedback (paraphrased):** Current submission reads as junior–mid level backend utility. Need: (1) a *product* that works end-to-end, (2) a *substantial* demo that feels real, (3) clearly justified technical decisions, (4) live-demo readiness. Assessor's two concrete suggestions: build a real **chunking + AI-metadata pipeline**, OR show **end-to-end RAG with a chat frontend**. This plan does **both**, tied together into a single demo narrative.

> **Constraint change:** No more Gemini API (credits exhausted). All LLM + embedding work moves to **100% local inference** via Ollama + sentence-transformers. This is also a narrative *upgrade*: the demo runs fully offline, zero quota risk, zero cost — a more defensible production story for a tax authority anyway (data sovereignty, no third-party LLM exposure).

> **Hardware constraint:** Must run on a **normal laptop** (8GB RAM, no GPU, no high-end CPU). Every model choice is sized for this. Total RAM budget during live demo: ~3.5 GB across all containers, leaving headroom for browser + IDE.

---

## Context

The project currently has a working CRAG backend (OpenSearch hybrid search + RRF, Gemini grader/generator, Redis cache, RBAC filtering, single-page HTML UI with pipeline trace). It runs in Docker Compose and responds to prefab demo queries on ~80 pre-embedded seed chunks. **All Gemini calls must be removed** — no free credits remain.

What the assessor flagged is real:
- The UI is a *diagnostic panel*, not a product. One-shot request/response, no chat, no streaming.
- There is no visible *ingestion* story — chunks arrive pre-computed, so the most interesting engineering (chunking, AI metadata enrichment) is invisible.
- "Advanced" techniques (HyDE, reranker, semantic cache, decomposition) exist only in pseudocode/prompts. The live demo cannot show any of them firing.
- Demo reliability is fragile: dependency on any remote API during the live walkthrough is a risk.

**Intended outcome:** A 10-minute live walkthrough where the assessor (a) chats with the system like a product, (b) uploads a real Dutch tax PDF and watches it get chunked + AI-enriched + indexed, (c) asks questions about the document they just uploaded, (d) sees visible justification for every pipeline decision, (e) trusts it will work — because *everything* runs locally in Docker, no network needed.

---

## Design pillars (what moves the needle vs. what's polish)

| Pillar | Moves us from junior → senior? | Priority |
|---|---|---|
| **Full Gemini → local stack migration (Ollama + sentence-transformers)** | **Yes** — unblocks everything; also a stronger production story | **P0** |
| Chat UI + SSE token streaming + conversation memory | **Yes** — this is "a product" vs. "a form" | **P0** |
| Document upload + live chunking pipeline + AI metadata | **Yes** — this *is* the assessor's explicit suggestion | **P0** |
| Local cross-encoder reranker (bge-reranker-base) | Yes — now affordable since we already run local inference | P1 |
| HyDE for SIMPLE queries (visible in trace) | Yes — turns pseudocode into a demo-able capability | P1 |
| "Why?" tooltips on every pipeline node | Yes — makes justification *visible*, not just written | P1 |
| Semantic cache (cosine ≥ 0.97) upgrade from hash-based | Nice-to-have | P2 |
| Eval page with gold Q&A metrics panel | Nice-to-have | P2 |

**Explicit non-goals** (deliberately descoped to protect demo reliability + laptop performance):
- **No remote LLM APIs** — no Gemini, no OpenAI, no Anthropic. Everything runs in Docker.
- **No models >2B parameters by default.** Default LLM: `qwen2.5:1.5b-instruct` (~1GB RAM at inference). Optional upgrade to `qwen2.5:3b-instruct` via env var for stronger machines.
- No multiple embedding models — lock to **one** tiny local model (`intfloat/multilingual-e5-small`, 384-dim, ~120MB). Mixing dims breaks kNN silently.
- **No separate cross-encoder reranker by default** — demoted from P1 to P2 because bge-reranker adds another ~280MB model + CPU load per query. Use **LLM-as-reranker** (the same small Qwen model) in P1 instead — zero extra memory, one single model family, visible in trace.
- No LangGraph — the imperative state machine is already cleaner and easier to trace.
- No auth/JWT — tier is still a request parameter; adding real auth is a red herring for this assessment.

**Memory budget (total ~3.5 GB):**
| Container | Memory |
|---|---|
| OpenSearch (heap 512MB + overhead) | ~1.0 GB |
| Ollama (qwen2.5:1.5b loaded) | ~1.2 GB |
| API (Python + torch-cpu + sentence-transformers) | ~0.9 GB |
| Redis | ~0.1 GB |
| Docker engine overhead | ~0.3 GB |
| **Total** | **~3.5 GB** |

Leaves ~4.5 GB free on an 8GB laptop for Chrome + VS Code + OS.

---

## Phased plan

### Phase 0 — Migrate to 100% local inference (3–4h, P0) ⭐ **foundation**

**Goal:** Zero remote API calls. Everything runs in Docker on the assessor's laptop.

1. **Tag current repo as `v1-submission`** before refactor so the Gemini version is preserved.
2. **Add Ollama to docker-compose** as a new service:
   - Image: `ollama/ollama:latest` (CPU-only — no GPU required)
   - Volume: `ollama_models:/root/.ollama` (persists model weights across restarts)
   - Pre-pull on first startup: **`qwen2.5:1.5b-instruct`** (~1GB, strong instruction-following, multilingual incl. Dutch) — default for laptop demos
   - Optional upgrade: `qwen2.5:3b-instruct` for stronger machines — select via `OLLAMA_MODEL` env var
   - Optional downgrade: `qwen2.5:0.5b-instruct` (~400MB) for very low-RAM machines
   - Memory cap: set `num_ctx=4096` (smaller context window reduces RAM) — enough for RAG (8 chunks × 500 chars + prompt)
   - Init container / startup script runs `ollama pull` so the model is ready before API starts
3. **Replace Gemini LLM calls** in `app/pipeline/llm.py`:
   - Swap `google.genai` for the `ollama` Python client
   - Same `generate(system_prompt, user_prompt, temperature)` signature so callers don't change
   - Use `/api/chat` endpoint with streaming support (needed for Phase 1)
   - Retry with exponential backoff on `ollama` model-loading delays
4. **Replace Gemini embeddings** with local `sentence-transformers`:
   - Model: `intfloat/multilingual-e5-small` (384-dim, ~120MB, works well for Dutch legal text)
   - Use `sentence-transformers` library (CPU inference is fast enough for demo — ~50ms/query)
   - Embed in the API container (no separate service needed)
   - Important: apply the e5 prefix convention — `"query: ..."` for queries, `"passage: ..."` for docs
5. **Re-embed seed data:** regenerate `seed_data/chunks.json` with 384-dim embeddings (old 3072-dim Gemini embeddings are unusable). Provide a one-shot script `scripts/rebuild_seed.py`.
6. **Update OpenSearch index mapping:** change `embedding` field dim from 3072 → 384. Recreate index on first startup (the existing setup.py re-creates if mapping differs).
7. **Config changes:**
   - Remove `gemini_api_key`, `gemini_llm_model`, `gemini_embedding_model` from `config.py`
   - Add `ollama_host=ollama`, `ollama_port=11434`, `ollama_model=qwen2.5:1.5b-instruct`
   - Add `ollama_num_ctx=4096` to cap context window (lower RAM, faster inference)
   - Add `embedding_model=intfloat/multilingual-e5-small`, `embedding_dim=384`
   - Add `opensearch_heap_mb=512` — explicitly sized for low-RAM laptops
8. **Update `.env.example`** — remove all Gemini keys, add Ollama model selection.
9. **Update README** — "No API keys required. Runs fully offline."

**Critical files:**
- [demo/docker-compose.yml](demo/docker-compose.yml) — add `ollama` service + init script
- [demo/app/pipeline/llm.py](demo/app/pipeline/llm.py) — full rewrite to Ollama client
- [demo/app/pipeline/embeddings.py](demo/app/pipeline/embeddings.py) (new) — sentence-transformers wrapper
- [demo/app/config.py](demo/app/config.py) — drop Gemini, add Ollama
- [demo/requirements-demo.txt](demo/requirements-demo.txt) — add `ollama`, `sentence-transformers`; remove `google-genai`
- [demo/seed_data/chunks.json](demo/seed_data/chunks.json) — regenerate with 384-dim
- [demo/scripts/rebuild_seed.py](demo/scripts/rebuild_seed.py) (new) — regeneration script
- [demo/.env.example](demo/.env.example) — drop Gemini, add Ollama model selection
- [demo/app/opensearch/setup.py](demo/app/opensearch/setup.py) — update embedding dim to 384

**Quality note:** Qwen 2.5 1.5B is much weaker than Gemini 3.1 Pro. For classification/grading/rewriting (structured, short outputs), it's adequate. For long-form Dutch tax explanations, responses will be short and sometimes repetitive — mitigate via tight system prompts with 1–2 few-shot examples, a "respond in ≤3 sentences" constraint, and always-on citation validation. The assessor's primary interest is the *architecture and decision-making*, not the raw answer fluency — and running entirely on-laptop with a small model is a feature, not a bug.

**Latency note:** Expected tokens/sec on typical laptop CPU (Intel i5 / Apple M1):
- qwen2.5:1.5b — 25–40 tok/s → ~2s for a 50-token answer
- qwen2.5:3b — 12–20 tok/s → ~3–4s for a 50-token answer
- e5-small embedding — ~50ms per query on CPU
- Full CRAG pipeline (classify + retrieve + grade + generate) — target ≤8s end-to-end with 1.5B model

---

### Phase 1 — Chat UI + streaming + conversation memory (4–6h, P0)

**Goal:** The assessor types, tokens stream in, follow-up questions work with context.

1. **Streaming endpoint:** add `POST /v1/chat/stream` returning `text/event-stream`. Emit SSE events at every CRAG state transition: `{type: "trace", node: "classify", ...}`, `{type: "trace", node: "retrieve", ...}`, then `{type: "token", text: "..."}` chunks during generation, then `{type: "citations", ...}` and `{type: "done"}`.
2. **Generator streaming:** switch `app/pipeline/generator.py:generate_response` to use Ollama's streaming API (`/api/chat` with `stream=true`). Accumulate full text server-side for cache + validation; emit tokens as they arrive. Ollama token latency on CPU is ~20–40 tok/s for a 3B model — fast enough that streaming feels live.
3. **Conversation memory:** new Redis key `chat:session:{session_id}:history` holding the last N turns (default 6). On each request, prepend a condensed summary of prior turns to the classifier + retriever inputs. TTL 1h.
4. **Rewrite classifier:** for follow-ups, classify as `FOLLOWUP` and use the previous turn's retrieved chunks as context for query rewriting before re-retrieval ("what about for self-employed?" → "what is the arbeidskorting for self-employed?").
5. **Chat UI:** replace [demo/app/static/index.html](demo/app/static/index.html) with a two-pane layout:
   - Left pane: chat thread (user bubbles + assistant bubbles with inline citation chips). Streaming tokens render in real-time. Each assistant bubble has a collapsible "pipeline trace" section underneath.
   - Right pane: live pipeline diagram that lights up as SSE trace events arrive. Tier selector, session controls (new chat, clear history), health dots.
6. **Keep** the existing `POST /v1/query` endpoint untouched as a fallback route.

**Critical files:**
- [demo/app/routers/query.py](demo/app/routers/query.py) — add new chat/stream router
- [demo/app/pipeline/crag.py](demo/app/pipeline/crag.py) — accept an async event callback so the state machine can emit SSE
- [demo/app/pipeline/generator.py](demo/app/pipeline/generator.py) — streaming variant
- [demo/app/static/index.html](demo/app/static/index.html) — chat UI rewrite

---

### Phase 2 — Live chunking + AI metadata + **visible hierarchy** pipeline (8–10h, P0)

**Goal:** Drag a Dutch tax PDF onto the UI, watch it get chunked, see the AI-extracted metadata + *visual hierarchical tree* of the document, query it, and see which tree nodes the retriever touched.

**Why hierarchy is called out explicitly:** the assessor's exact phrasing was "metadata voor hiërarchische relaties". The existing [schemas/chunk_metadata.json](schemas/chunk_metadata.json) already defines the hierarchy contract — `parent_chunk_id`, `hierarchy_path`, `chapter`/`section`/`article_num`/`paragraph_num`/`sub_paragraph`, deterministic `chunk_id` format `{doc_id}::{article}::{paragraph}::{chunk_seq}`. And [pseudocode/module1_ingestion.py](pseudocode/module1_ingestion.py) already has the structural boundary detector for Dutch legal hierarchy (Hoofdstuk > Afdeling > Artikel > Lid > Sub). What's missing is **wiring it through the live pipeline and showing it to the assessor**.

1. **Upload endpoint:** `POST /v1/ingest` accepting multipart PDF/TXT/MD + tier + doc_type. Returns `ingestion_id`; progress streams back via `GET /v1/ingest/{id}/stream` (SSE).
2. **Chunking pipeline** in new `app/ingestion/` module:
   - `parser.py` — pdfplumber for PDFs, plain read for text. Preserve page numbers.
   - `structural_chunker.py` — split on Dutch legal structural markers: `Hoofdstuk`, `Afdeling`, `Artikel`, numbered paragraphs. Fall back to recursive char splitter at 800 tokens with 120-token overlap when no markers present.
   - `metadata_enricher.py` — **Ollama** call per chunk using a strict JSON-schema prompt with `format: "json"` mode (Ollama guarantees valid JSON). Extract: `hierarchy_path`, `article_ref`, `article_num`, `summary` (1-sentence), `topics` (3–5 tags), `entities` (laws, articles, ECLI refs), `effective_date` if present, `language`. This is the **AI metadata** the assessor asked for. Parallelize with `asyncio.gather` (batch size 4 to avoid Ollama CPU saturation).
   - `embedder.py` — batch **sentence-transformers** embeddings with "passage: " prefix. Runs on CPU in the API container.
   - `indexer.py` — bulk-index to OpenSearch with tier = upload tier.
3. **Progress UI:** dropzone in chat UI → toast showing live progress bar (`parsed → hierarchy detected (N nodes) → chunked (N) → enriched (N/M) → embedded → indexed`). When done, toast becomes "✅ Indexed N chunks — explore structure or try asking about it" with a sample question generated from the extracted topics.

4. **Hierarchical tree view** *(this is the the assessor-facing moment)* — new `GET /v1/doc/{doc_id}/tree` returns nested JSON built from the `parent_chunk_id` relationships:
   ```
   Document (AWR-2024-v3)
   ├── Hoofdstuk 3 — Heffingskorting
   │   ├── Afdeling 1
   │   │   ├── Art 3.114 — Arbeidskorting
   │   │   │   ├── Lid 1 [chunk001]
   │   │   │   ├── Lid 2 [chunk002]
   │   │   │   │   ├── Sub a [chunk003]
   │   │   │   │   └── Sub b [chunk004]
   │   │   │   └── Lid 3 [chunk005]
   │   │   └── Art 3.115 [chunk006]
   │   └── Afdeling 2 ...
   └── Hoofdstuk 4 ...
   ```
   Rendered in the UI as a **collapsible, interactive tree** (vanilla JS `<details>`/`<summary>` — zero dependencies). Each leaf node is a clickable chunk showing:
   - Badge with `article_num` / `paragraph_num` / `sub_paragraph`
   - Token count + security tier color
   - Click → side panel shows full metadata JSON + chunk text + "Why this chunk exists here" (AI-generated 1-line explanation from the enricher)

5. **Retrieval ↔ tree highlight** *(senior-level demo moment)* — when the user asks a question, the tree nodes whose chunks were retrieved **light up** with a pulse animation. The tree nodes that were graded RELEVANT get a green outline; AMBIGUOUS chunks get amber; the chunks actually cited in the final answer get a 🎯 badge. This visually proves the retrieval → grading → citation chain uses the hierarchy.

6. **Hierarchical context expansion in retrieval** — when a paragraph-level chunk is graded RELEVANT, retrieve its parent article-level chunk (via `parent_chunk_id`) and include it in the context for the generator. This is a real capability unlocked by hierarchical metadata, not a UI trick. Trace event: `hierarchy_expand` with "added 2 parent chunks for fuller context". Goes beyond flat RAG — classic senior signal.

7. **Metadata viewer modal:** clicking the ✅ toast opens a modal showing a **tabbed view**:
   - Tab 1: the hierarchical tree
   - Tab 2: flat chunk list with inline metadata JSON (the full 22 fields per chunk)
   - Tab 3: a heat-map grid showing AI-extracted `topics` frequency across chunks (shows the *semantic* structure on top of the *syntactic* tree)

8. **Safety:** enforce max file size (10MB), max pages (50), max chunks (200). Reject duplicates by content hash. Tree depth capped at 6 levels to keep the UI readable.

**Critical files (all new):**
- [demo/app/ingestion/](demo/app/ingestion/) — full module (parser, structural_chunker, metadata_enricher, embedder, indexer, **tree_builder**)
- [demo/app/ingestion/tree_builder.py](demo/app/ingestion/tree_builder.py) — builds nested tree JSON from flat chunks via `parent_chunk_id`
- [demo/app/routers/ingest.py](demo/app/routers/ingest.py) — upload + SSE progress
- [demo/app/routers/docs.py](demo/app/routers/docs.py) — `GET /v1/doc/{doc_id}/tree`, `GET /v1/doc/{doc_id}/chunk/{chunk_id}`
- [demo/app/pipeline/chunking_prompts.py](demo/app/pipeline/chunking_prompts.py) — structured-output metadata prompt (Ollama JSON mode)
- [demo/app/static/index.html](demo/app/static/index.html) — dropzone + progress UI + tree view + retrieval-highlight overlay
- [demo/app/static/tree.css](demo/app/static/tree.css) — tree styling (pulse animation, tier colors, RELEVANT/CITED badges)

**Existing references to reuse (port, don't reinvent):**
- [pseudocode/module1_ingestion.py](pseudocode/module1_ingestion.py) — structural boundary detector for Dutch legal hierarchy (lines ~220–290). Port the `detect_legal_boundaries()` and `propagate_hierarchy()` logic directly.
- [schemas/chunk_metadata.json](schemas/chunk_metadata.json) — the 22 metadata fields + the `_design_notes.parent_child_hierarchy` contract. Use as the enricher's JSON output schema.
- Deterministic chunk_id format from schema: `{doc_id}::{article}::{paragraph}::{chunk_seq}` — this *is* the tree structure encoded in the ID. Makes re-indexing idempotent and parent lookup O(1).

---

### Phase 3 — Advanced retrieval (visible, P1)

Goal: turn pseudocode-only features into trace-visible pipeline stages — without adding new models.

**3a. LLM-as-reranker (1h)** — *preferred over a dedicated cross-encoder for laptop deployment*
- After RRF fusion returns top-K, send all K chunks to **the same Ollama model** in one batched prompt asking for a relevance-ranked JSON list (`format: "json"` mode for guaranteed parsing).
- Keep top-K_rerank (default 4).
- Trace event: `rerank` with duration and pre/post ordering visualization.
- **Why this instead of bge-reranker:** zero new models, zero new memory, one consistent model family. Cross-encoders give 2–3% better nDCG but require 280MB extra RAM and a separate inference path — bad trade on an 8GB laptop. Pseudocode/write-up can still describe bge-reranker as the production choice.

**3b. HyDE for SIMPLE queries (1–2h)**
- In `retriever.py`, when `query_type == "SIMPLE"` and confidence < threshold, synthesize a hypothetical answer via Ollama, embed it with e5-small, use *that* vector for kNN. Keep BM25 on the original query.
- Trace event shows both the original query and the hypothetical document.

**3c. Semantic cache (optional, 1h)**
- Replace hash-based cache key with embedding-based lookup: compute query embedding (already free — same e5-small we're using for retrieval), HNSW search in Redis Stack, return cache hit if cosine ≥ 0.97.
- Trace event: `cache_lookup` now shows similarity score, not just HIT/MISS.
- Essentially free to add since embeddings are already being computed on the retrieval path.

**Critical files:**
- [demo/app/pipeline/retriever.py](demo/app/pipeline/retriever.py)
- [demo/app/pipeline/reranker.py](demo/app/pipeline/reranker.py) (new, uses Ollama — no new model)
- [demo/app/pipeline/cache.py](demo/app/pipeline/cache.py)

---

### Phase 4 — Visible justification (2–3h, P1)

**Goal:** Every pipeline node the assessor clicks explains *why it exists* and *what alternatives were considered*.

1. Each pipeline node in the UI gets an info icon. Hover → tooltip with: purpose, alternatives considered, why this choice, cost/latency impact.
2. Content comes from a new [demo/app/static/justifications.json](demo/app/static/justifications.json) sourced from `drafts/final_submission_v2.md` — single source of truth, no duplication.
3. Each tooltip includes a "See in pseudocode →" link jumping to the canonical implementation file (e.g. `pseudocode/module2_retrieval.py:519`).

---

### Phase 5 — Seed data expansion (2h, P1)

**Goal:** The demo corpus reflects real-world diversity the assessor can probe.

Use the Phase 2 chunking pipeline itself to ingest ~5 real Dutch tax documents (Wet IB excerpts, Handboek Invordering snippets, 2–3 ECLI cases, 1 transfer pricing ruling, 1 FIOD procedure). Target ~150 chunks total across all four tiers. This also serves as a **self-test** of Phase 2.

**Verification artifact:** `seed_data/ingestion_report.json` showing chunk counts per tier, metadata field coverage, embedding dim consistency.

---

### Phase 6 — Eval page (optional, 2h, P2)

Hosted at `/eval` — runs the golden Q&A set from [eval/golden_qa_sample.json](eval/golden_qa_sample.json), shows context recall, answer faithfulness, citation precision as a live dashboard. Only build if P0 + P1 are solid.

---

## Risk mitigations

| Risk | Mitigation |
|---|---|
| Ollama model not pre-pulled → first request hangs | Init script pulls model at container build; API blocks on `/health` until Ollama reports ready; show "Warming up models..." in UI |
| Laptop runs out of RAM | Default `qwen2.5:1.5b-instruct` (~1.2GB loaded) + e5-small (120MB) + OpenSearch heap 512MB → ~3.5GB total. Fits comfortably on 8GB laptops. Env var `OLLAMA_MODEL=qwen2.5:0.5b-instruct` for <8GB machines |
| Slow CPU inference makes demo feel sluggish | SSE token streaming makes even slow generation *feel* fast — tokens arrive visibly. Plus: low `num_ctx=4096`, answer-length cap ≤3 sentences, classify/grade prompts kept tiny |
| SSE breaks behind corporate proxy | Keep `POST /v1/query` endpoint as synchronous fallback; UI detects and degrades gracefully |
| PDF parsing edge case crashes ingest | Wrap each chunk step in try/except, emit `failed` SSE event, keep already-indexed chunks |
| Embedding dim mismatch (old 3072 Gemini data in index) | Force index recreation on first startup after migration; assert dim=384 on every insert |
| Qwen 3B produces weaker Dutch responses than Gemini | Tight system prompts with 1–2 few-shot examples; validator catches citation failures and refuses |
| Docker image bloat | sentence-transformers + torch-cpu ~ 600MB extra — acceptable. bge-reranker only downloaded at runtime into a volume |
| Conversation memory pollutes retrieval | Store condensed summary, not raw turns; cap at 6 turns; clear button in UI |
| Regression in existing flow | Keep `POST /v1/query` untouched; all new work on new routes; feature flags via config |

---

## Time budget

| Tier | Phases | Total |
|---|---|---|
| **Minimum viable senior demo** | 0 + 1 + 2 | 15–20h |
| **Recommended** | 0 + 1 + 2 + 3a + 3b + 4 + 5 | 22–29h |
| **Full** | all phases | 26–33h |

Target: Recommended tier. Phase 6 only if time allows after dress-rehearsal. Phase 0 adds 3–4h vs. the previous (Gemini) plan because the full LLM + embedding stack has to be swapped and seed data re-embedded. Phase 2 is +2h for the hierarchical tree view + retrieval highlighting (the direct answer to the assessor's "hiërarchische relaties" comment).

---

## Demo narrative (the 10-minute walkthrough)

1. **Open chat** → "What is the arbeidskorting for 2025?" → tokens stream in with inline citations; pipeline diagram lights up left-to-right. *Point at trace: classify → HyDE → retrieve → rerank → grade → generate → validate.*
2. **Follow-up** → "And for self-employed people?" → query rewriter uses conversation context; retrieve fires again. *Point at rewrite node showing the expanded query.*
3. **Drag a PDF** onto the dropzone (`wet-ib-artikel-3.pdf`). Watch the progress bar: parsed → **hierarchy detected: 2 chapters, 5 sections, 14 articles, 32 paragraphs** → chunked → AI-enriched (hierarchy_path, topics, entities visible) → embedded → indexed. Toast: "✅ Indexed 32 chunks — explore structure or ask about Hoofdstuk 3."
3b. **Click "Explore structure"** → modal opens showing the collapsible tree of the just-ingested document. Expand `Hoofdstuk 3 > Afdeling 1 > Art 3.114`, see the 5 paragraph-level chunks underneath. Click one → side panel shows the chunk text, AI-extracted topics, and its `hierarchy_path`. *"This is the metadata voor hiërarchische relaties the assessor asked for — built live, 30 seconds ago."*
4. **Ask about the uploaded doc** ("Wat is arbeidskorting lid 2?"). Answer cites chunks created 30 seconds ago. In the tree view, **watch the nodes light up**: retrieved nodes pulse, RELEVANT nodes get green outlines, the cited chunks get 🎯 badges. The pipeline trace shows `hierarchy_expand: added 2 parent article chunks`. *This is the moment.*
5. **Switch tier** PUBLIC → CLASSIFIED_FIOD → same query returns additional FIOD procedural chunks. RBAC visible.
6. **Ask an irrelevant question** ("Who built the Eiffel Tower?") → IRRELEVANT grade → REFUSE state → Dutch refusal message. Guardrail visible.
7. **Re-ask question 1** → semantic cache HIT at similarity 0.99, 10ms response. *Point at cache lookup node showing the similarity score.*
8. **Hover any pipeline node** → tooltip explains *why* that node exists, what alternatives were considered, link to pseudocode.

---

## Critical files summary

| Status | Path | Purpose |
|---|---|---|
| **NEW** | [demo/app/ingestion/](demo/app/ingestion/) | Chunking + AI metadata pipeline |
| **NEW** | [demo/app/ingestion/tree_builder.py](demo/app/ingestion/tree_builder.py) | Flat chunks → nested tree via `parent_chunk_id` |
| **NEW** | [demo/app/routers/ingest.py](demo/app/routers/ingest.py) | Upload + progress SSE |
| **NEW** | [demo/app/routers/docs.py](demo/app/routers/docs.py) | `GET /v1/doc/{doc_id}/tree` + chunk detail |
| **NEW** | [demo/app/routers/chat.py](demo/app/routers/chat.py) | `/v1/chat/stream` endpoint |
| **NEW** | [demo/app/static/tree.css](demo/app/static/tree.css) | Tree view styling + pulse/highlight animations |
| **NEW** | [demo/app/pipeline/embeddings.py](demo/app/pipeline/embeddings.py) | Local sentence-transformers wrapper |
| **NEW** | [demo/app/pipeline/reranker.py](demo/app/pipeline/reranker.py) | LLM-as-reranker via Ollama (no new model) |
| **NEW** | [demo/app/static/justifications.json](demo/app/static/justifications.json) | Tooltip content |
| **NEW** | [demo/scripts/rebuild_seed.py](demo/scripts/rebuild_seed.py) | Re-embed seed data with e5-small |
| **REWRITE** | [demo/app/pipeline/llm.py](demo/app/pipeline/llm.py) | Gemini → Ollama client |
| **REWRITE** | [demo/app/static/index.html](demo/app/static/index.html) | Chat UI + dropzone + live pipeline |
| **REWRITE** | [demo/seed_data/chunks.json](demo/seed_data/chunks.json) | Re-embedded with 384-dim vectors |
| **EDIT** | [demo/docker-compose.yml](demo/docker-compose.yml) | Add `ollama` service + model pre-pull |
| **EDIT** | [demo/app/pipeline/crag.py](demo/app/pipeline/crag.py) | Emit SSE events at each transition |
| **EDIT** | [demo/app/pipeline/generator.py](demo/app/pipeline/generator.py) | Streaming generation via Ollama |
| **EDIT** | [demo/app/pipeline/retriever.py](demo/app/pipeline/retriever.py) | HyDE + reranker hooks, local embeddings |
| **EDIT** | [demo/app/pipeline/cache.py](demo/app/pipeline/cache.py) | Semantic (embedding) cache — 384-dim |
| **EDIT** | [demo/app/opensearch/setup.py](demo/app/opensearch/setup.py) | Embedding dim 3072 → 384, force recreate |
| **EDIT** | [demo/app/config.py](demo/app/config.py) | Remove Gemini, add Ollama + embedding model |
| **EDIT** | [demo/.env.example](demo/.env.example) | Remove API key, add Ollama model selector |
| **EDIT** | [demo/requirements-demo.txt](demo/requirements-demo.txt) | Remove `google-genai`; add `ollama`, `sentence-transformers`, `pdfplumber`, `sse-starlette` |
| **EDIT** | [demo/README.md](demo/README.md) | "No API keys required — runs fully offline" |

---

## Verification

Per-phase smoke tests:

- **Phase 0:** `docker-compose up` with network disconnected → all 6 demo queries succeed. `docker exec -it api curl http://ollama:11434/api/tags` shows qwen2.5:3b. OpenSearch index has 384-dim embeddings (`curl localhost:9200/tax_authority_rag_chunks/_mapping`).
- **Phase 1:** Open chat, send "Wat is de arbeidskorting?" → tokens stream one at a time, not all at once. Send "And for self-employed?" → rewritten query visible in trace.
- **Phase 2:** Drag a 5-page PDF → progress bar advances through all stages (including "hierarchy detected: N chapters / M articles") → ≥10 chunks indexed → query about a topic from that PDF returns citations from the just-uploaded chunks. Tree view opens → every chunk has a non-empty `hierarchy_path` and a valid `parent_chunk_id` chain back to the root. Asking a paragraph-level question → retrieval trace shows `hierarchy_expand` fetched the parent article chunk. Tree nodes pulse when retrieved; cited nodes show 🎯.
- **Phase 3a:** Trace shows `rerank` node with score-reordering visualization.
- **Phase 3b:** SIMPLE query trace shows `hyde` node with the synthesized hypothetical doc.
- **Phase 3c:** Second near-duplicate query ("What's the arbeidskorting?" vs "Wat is arbeidskorting?") → cache HIT at cosine > 0.97.
- **Phase 4:** Hover every pipeline node → every one has a non-empty tooltip.
- **Phase 5:** `ingestion_report.json` shows ~150 chunks, metadata coverage ≥95% per field, dim=3072 across all.

End-to-end acceptance: **physically disconnect the network**, run the demo narrative above — all 8 steps succeed. No Gemini, no remote calls, no API keys.
