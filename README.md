# Enterprise RAG Architecture — Dutch Tax Authority

Technical assessment by **Emre Ram**.

> **Language note:** the **product UI is Dutch** (it simulates a tool for Dutch Tax Authority — *Belastingdienst* — civil servants). All assessor-facing material — this README, the demo script, the slide deck, code, logs and docstrings — is **English**. Internal working notes (file names ending in `_PLAN.md` and `ASSESSMENT_REVIEW_FEEDBACK.md`) remain in Dutch as candidate-side scratchpads; they are not required reading.

---

## Two layers in this repo

- **Live demo** in [`demo/`](demo/) — a working product that runs on a laptop, fully offline once warmed up. This is what you see during the conversation.
- **Written architecture** in [`drafts/`](drafts/), [`pseudocode/`](pseudocode/), [`schemas/`](schemas/), [`diagrams/`](diagrams/), [`performance/`](performance/) — the production-scale design document (20M chunks, GPU cluster). The demo is a downsized implementation; deviations are listed in the banner of [`drafts/final_submission_v2.md`](drafts/final_submission_v2.md).

---

## Start here

1. **Open the deck:** [`assessment_AI_USE_emresemerci_v2.pptx`](assessment_AI_USE_emresemerci_v2.pptx). 19 slides covering the full AI-assisted workflow, architecture, and runnable demo.
2. **Justification notes per Operations tab:** [`slides/operations_justification.md`](slides/operations_justification.md) — five sections covering Choice · Rejected · Trade-off for each Operations workspace in the demo. (Render to .pptx with `python slides/build_slides.py` if a slide deck is preferred.)
3. **Main document:** [`drafts/final_submission_v2.md`](drafts/final_submission_v2.md). Four modules in depth.
4. **Live demo:** see instructions below.

## Live demo (Docker)

Requirements: **Docker Desktop 4.40+** with **Model Runner** enabled (under *Settings → Features in development → Beta features → Enable Docker Model Runner*). No API keys, no network calls at runtime.

```bash
cd demo
docker compose up -d
# Wait ~30 seconds for warmup (embedder + index + cache).
# Then open:
open http://localhost:8000
```

The first run pulls `ai/gemma4:E2B` (~1.5 GB) via Model Runner. Subsequent starts are instant.

**What you see in the browser** (UI is Dutch by design):

- **Werkruimte** (end-user workspace): Gesprek (chat) + Documenten (documents).
- **Operations** (operator/engineer): Ingestie · Retrieval · CRAG-pipeline · Toegang · Kwaliteit (Ingestion · Retrieval · CRAG · Access · Quality).
- Switch role in the top-left (Publiek / Juridisch medewerker / Inspecteur / FIOD-rechercheur) to see RBAC enforce live.

**Demo flow:** [`demo/DEMO_SCRIPT.md`](demo/DEMO_SCRIPT.md) — 8 acts of ~70 seconds each.

## Where the assessment maps to the code

| Module from `assesment.txt` | Implementation |
|---|---|
| **Module 1 — Ingestion & Knowledge Structuring** | Hierarchical structural chunker with semantic fallback, deterministic `chunk_id`, parent_chunk_id metadata — see [`demo/app/ingestion/`](demo/app/ingestion/) and [`demo/app/opensearch/setup.py`](demo/app/opensearch/setup.py) (HNSW m=16, ef_construct=128, 384-dim e5-small). Quantization projection panel: Operations → Ingestie. |
| **Module 2 — Retrieval Strategy** | Hybrid BM25 + kNN with RRF fusion (k=60), HyDE for terse queries, query decomposition for multi-aspect questions, optional LLM rerank — see [`demo/app/pipeline/retriever.py`](demo/app/pipeline/retriever.py), [`demo/app/pipeline/hyde.py`](demo/app/pipeline/hyde.py), [`demo/app/pipeline/classifier.py`](demo/app/pipeline/classifier.py). Live trace at Operations → Retrieval. |
| **Module 3 — Agentic RAG & Self-Healing** | 9-state CRAG control loop with grader, rewrite-and-retry on AMBIGUOUS or IRRELEVANT, AMBIGUOUS-promotion fallback, citation-validator (fail-closed) — see [`demo/app/routers/chat.py`](demo/app/routers/chat.py), [`demo/app/pipeline/grader.py`](demo/app/pipeline/grader.py), [`demo/app/pipeline/refuse_classifier.py`](demo/app/pipeline/refuse_classifier.py). Visual at Operations → CRAG-pipeline. |
| **Module 4 — Production Ops, Security & Evaluation** | Tier-partitioned semantic cache with cosine ≥ 0.97 ([`demo/app/pipeline/cache.py`](demo/app/pipeline/cache.py)); pre-retrieval RBAC filter ([`demo/app/security/rbac.py`](demo/app/security/rbac.py)); Ragas + DeepEval golden-set evaluation ([`demo/app/eval/`](demo/app/eval/)). Plus reliability: circuit breaker, request-ID propagation, /readyz polling, refuse classification — see [`RELIABILITY_PLAN.md`](RELIABILITY_PLAN.md) (Dutch internal notes). |

## Live demo stack

| Component | Choice | Why (one line) |
|---|---|---|
| Inference | Docker Model Runner · `ai/gemma4:E2B` | Local, no API key, OpenAI-compatible endpoint |
| Embeddings | `intfloat/multilingual-e5-small` (384-dim) | CPU-fast, multilingual incl. Dutch |
| Vector + BM25 | OpenSearch 2.15 · HNSW (m=16, ef=128) | One engine for hybrid search + filter |
| Cache | Redis Stack | Tier-partitioned, semantic (cosine ≥ 0.97) |
| API | FastAPI + SSE | Streaming chat, live trace per turn |
| Frontend | Tailwind · vanilla JS | No build step, single HTML + JS file |

Full dependency list: [`demo/requirements-demo.txt`](demo/requirements-demo.txt).

## Production architecture (paper version)

The design in the drafts folder is built for **20M chunks** on a **3-node OpenSearch cluster + GPU LLM** (Mixtral / Llama 3.1 70B). The demo uses a lighter stack so it runs on a regular laptop. The architectural choices (RRF k=60, pre-retrieval RBAC, MAX_RETRIES=1, CRAG grading, parent-expansion, semantic cache) are identical between the two.

Supporting artefacts:
- [`pseudocode/`](pseudocode/) — 5 files: ingestion, retrieval, CRAG, grader, cache
- [`schemas/`](schemas/) — chunk metadata (22 fields), OpenSearch index mapping, RBAC roles
- [`diagrams/`](diagrams/) — architecture, retrieval flow, CRAG states, security model
- [`prompts/`](prompts/) — grader / generator / HyDE / decomposition prompt templates
- [`eval/`](eval/) — golden test set spec + metrics matrix
- [`performance/resource_allocation.md`](performance/resource_allocation.md) — sizing and cost-per-query at production scale
- [`reference/assumptions.md`](reference/assumptions.md) — A1–A18 explicit assumptions

## Repository files

| File | Purpose |
|---|---|
| [`assesment.txt`](assesment.txt) | The original assignment as received |
| [`README.nl.md`](README.nl.md) | Dutch snapshot of this README (kept for reference) |
| [`ASSESSMENT_REVIEW_FEEDBACK.md`](ASSESSMENT_REVIEW_FEEDBACK.md) | Verbatim feedback from the first assessment round (Dutch) |
| [`SENIOR_LEVEL_PLAN.md`](SENIOR_LEVEL_PLAN.md) | Plan for the post-feedback refactor (Dutch internal notes) |
| [`RELIABILITY_PLAN.md`](RELIABILITY_PLAN.md) | Failure-mode analysis + 5-sprint hardening plan (Dutch internal notes) |
| [`OUTDATED_AUDIT.md`](OUTDATED_AUDIT.md) | What in this repo is v1-design vs v3-implementation (Dutch internal notes) |
| [`CLAUDE.md`](CLAUDE.md) | Behavioral guidelines kept while iterating |
