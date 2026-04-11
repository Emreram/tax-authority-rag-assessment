# Architecture Overview — Tax Authority RAG System

> This is the **anchor diagram** for the submission. It shows the complete data
> flow from user query to final response, the ingestion pipeline that feeds the
> knowledge base, the component grid, and the latency budget that justifies the
> 1.5-second TTFT requirement (Assumption A13).
>
> Every other diagram in `diagrams/` drills into one section of this overview:
>   - Retrieval detail → [retrieval_flow.md](retrieval_flow.md)
>   - CRAG state machine → [crag_state_machine.md](crag_state_machine.md)
>   - Security & RBAC → [security_model.md](security_model.md)

---

## 1. System Summary

The system is a Retrieval-Augmented Generation (RAG) platform serving the Dutch
National Tax Authority. It must answer tax-law questions grounded in ~500,000
legal documents (~20 million chunks) for three user personas — tax inspectors,
legal counsel, and helpdesk staff — with:

- **Zero-hallucination tolerance** (Assumption A14): the system may refuse, never fabricate
- **Exact citations** (Assumption A12): every factual claim cites a specific article + paragraph
- **Strict RBAC** (Assumption A17): helpdesk staff must never retrieve from CLASSIFIED_FIOD documents
- **TTFT p95 < 1500 ms** (Assumption A13): hard latency budget measured at the 95th percentile
- **Data sovereignty** (Assumptions A1, A2): all components self-hosted on government cloud

---

## 2. High-Level Data Flow (Online Path)

This is the path taken by every user query at runtime.

```
                      User (browser / CLI / IDE plugin)
                                    │
                                    │  query + JWT (OIDC from AD/ADFS)
                                    ▼
                  ┌─────────────────────────────────────────┐
                  │  API Gateway (FastAPI + async)          │
                  │  - Validate JWT                         │
                  │  - Extract idp_groups → security_tier   │
                  │  - Rate limiting, request logging       │
                  └─────────────────────────────────────────┘
                                    │
                                    │  (query, user_security_tier, session_id)
                                    ▼
                  ┌─────────────────────────────────────────┐
                  │  Semantic Cache Layer                   │
                  │  (Redis Stack + RediSearch HNSW)        │
                  │  - Embed query (multilingual-e5-large)  │
                  │  - KNN lookup in tier-partitioned index │
                  │  - Threshold: cosine ≥ 0.97             │
                  │  - Tier filter: tier ≤ user_tier        │
                  └─────────────────────────────────────────┘
                        │                           │
                    HIT │ (~15 ms)              MISS│
                        ▼                           ▼
                    RESPOND              ┌──────────────────────────┐
                    (cached answer)      │  CRAG State Machine      │
                                         │  (LangGraph StateGraph)  │
                                         │                          │
                                         │  RECEIVE_QUERY            │
                                         │      ↓ classify_query()   │
                                         │  TRANSFORM_QUERY          │
                                         │      ↓ HyDE / decompose   │
                                         │  RETRIEVE  ◄──────┐       │
                                         │      ↓            │       │
                                         │  GRADE_CONTEXT    │       │
                                         │      ↓            │ retry │
                                         │  (RELEVANT?)      │ ≤ 1   │
                                         │      ├──AMBIG ────┘       │
                                         │      ├──IRREL → REFUSE    │
                                         │      └──RELEV             │
                                         │      ↓                    │
                                         │  GENERATE  (LLM @ T=0.0)  │
                                         │      ↓                    │
                                         │  VALIDATE_OUTPUT           │
                                         │  (citation set-membership) │
                                         │      ↓                    │
                                         │  RESPOND / REFUSE          │
                                         └──────────────────────────┘
                                                    │
                                                    │ uses
                                                    ▼
                  ┌─────────────────────────────────────────┐
                  │  Hybrid Retrieval Service               │
                  │  (OpenSearch 2.15+ with k-NN plugin)    │
                  │                                         │
                  │  ┌─────────────────────────────────┐    │
                  │  │ DLS Pre-Filter (RBAC)           │    │
                  │  │ user_security_tier → DLS role   │    │
                  │  │ search space = S \ S_forbidden  │    │
                  │  └─────────────────────────────────┘    │
                  │              │                          │
                  │     ┌────────┴─────────┐                │
                  │     │                  │                │
                  │ BM25 top-20       kNN top-20            │
                  │ (sparse)          (dense HNSW)          │
                  │     │                  │                │
                  │     └────────┬─────────┘                │
                  │        RRF fusion (k=60)                │
                  │              │                          │
                  │        top-40 → cross-encoder rerank    │
                  │        (BAAI/bge-reranker-v2-m3)        │
                  │              │                          │
                  │           top-8                         │
                  └─────────────────────────────────────────┘
                                    │
                                    ▼
                         Response to user
                         (answer + inline citations + source list)
                                    │
                                    ▼
                  ┌─────────────────────────────────────────┐
                  │  Observability Fan-Out (async)           │
                  │  - OpenTelemetry trace → Jaeger          │
                  │  - Prometheus metrics (latency, counts)  │
                  │  - Structured JSON logs → OpenSearch     │
                  │  - LLM call logs → LangSmith / Phoenix   │
                  └─────────────────────────────────────────┘
```

---

## 3. Component Grid

One row per major component, grouped by pipeline stage.

| Stage | Component | Technology | Purpose | Module |
|---|---|---|---|---|
| Ingress | API Gateway | FastAPI + Uvicorn | Async HTTP, JWT validation, rate limiting | Module 4 |
| Auth | Identity Provider | AD / ADFS / Azure AD OIDC | Organizational user management (existing) | Module 4 |
| Cache | Semantic Cache | Redis Stack + RediSearch | Tier-partitioned KNN cache, 0.97 threshold | Module 4 |
| Orchestration | CRAG Orchestrator | LangGraph StateGraph | 9-state machine with conditional edges | Module 3 |
| Query analysis | Query Classifier | Regex + LLM | Detects REFERENCE / SIMPLE / COMPLEX | Module 3 |
| Query transform | HyDE Generator | LLM (T=0.3) | Hypothetical passage generation for dense retrieval | Module 3 |
| Query transform | Decomposer | LLM (T=0.3) | Splits COMPLEX queries into ≤3 sub-queries | Module 3 |
| Retrieval | Hybrid Retriever | OpenSearch k-NN + BM25 | Three-path retrieval (exact-ID / sparse / dense) | Module 2 |
| Retrieval | Embedding Service | multilingual-e5-large (GPU) | 1024-dim query embeddings | Modules 1, 2 |
| Retrieval | Reranker | BAAI/bge-reranker-v2-m3 (GPU) | Cross-encoder rerank 40 → 8 | Module 2 |
| Safety gate | Retrieval Grader | LLM (batch call) | RELEVANT / AMBIGUOUS / IRRELEVANT | Module 3 |
| Generation | Generator LLM | Mixtral 8x22B or Azure OpenAI GPT-4 Gov | Grounded answer synthesis at T=0.0 | Module 3 |
| Safety gate | Citation Validator | Python set-membership | Verifies cited chunk_ids exist in graded context | Module 3 |
| Storage | Chunk Index | OpenSearch (HNSW m=16, SQ8) | 20M-chunk vector + BM25 + DLS index | Modules 1, 4 |
| Storage | Audit Log Index | OpenSearch (separate index) | Full query/response audit trail | Module 4 |
| Observability | Tracing | OpenTelemetry → Jaeger | Distributed traces per query | Module 4 |
| Observability | Metrics | Prometheus + Grafana | TTFT, cache hit rate, error rate | Module 4 |
| Observability | LLM Observability | LangSmith or Arize Phoenix | Prompt/response logs, cost tracking | Module 4 |
| Evaluation | Eval Pipeline | Ragas + DeepEval + pytest | Offline gate on deploys | Module 4 |

---

## 4. Ingestion Pipeline (Offline Path)

The ingestion pipeline runs as a batch job (nightly or on-change), separate from
the online query path. It is the only write path to the OpenSearch index. The
cache is not written directly — it is invalidated via callback when documents
are re-indexed (see `module4_cache.py:on_documents_reindexed()`).

```
  Source Documents
  (PDF / HTML / XML from wetten.overheid.nl, rechtspraak.nl,
   internal CMS, FIOD document stores)
                │
                ▼
  ┌──────────────────────────────────────────────┐
  │ Document Loader                              │
  │  - PDF extraction (pdfplumber / unstructured)│
  │  - HTML parsing (lxml)                        │
  │  - XML parsing (for wetten.overheid.nl)       │
  │  - Initial metadata capture (source_url,     │
  │    doc_type, security_classification)        │
  └──────────────────────────────────────────────┘
                │
                ▼
  ┌──────────────────────────────────────────────┐
  │ LegalDocumentChunker                          │
  │  (LlamaIndex custom NodeParser)               │
  │                                               │
  │  1. Detect structure via regex:               │
  │     - Hoofdstuk (Chapter)                     │
  │     - Afdeling (Section)                      │
  │     - Artikel (Article)                       │
  │     - Lid (Paragraph)                         │
  │  2. Split on structural boundaries (NEVER    │
  │     mid-article, NEVER mid-paragraph)         │
  │  3. Propagate parent metadata to children     │
  │  4. Build hierarchy_path                      │
  │  5. Generate deterministic chunk_id           │
  │     ({doc_id}::{article}::{lid}::{chunk_seq}) │
  │  6. Create parent-child NodeRelationship      │
  └──────────────────────────────────────────────┘
                │
                ▼
  ┌──────────────────────────────────────────────┐
  │ Temporal Versioning Stamp                     │
  │  - effective_date = publication_date          │
  │  - expiry_date = next_version_effective OR null│
  │  - version = monotonic integer                │
  └──────────────────────────────────────────────┘
                │
                ▼
  ┌──────────────────────────────────────────────┐
  │ Embedding Generation (GPU batch)              │
  │  - multilingual-e5-large                      │
  │  - Prefix: "passage: " (E5 convention)        │
  │  - Output: 1024-dim fp32 → SQ8 on write       │
  │  - Batched (64 chunks per GPU call)           │
  └──────────────────────────────────────────────┘
                │
                ▼
  ┌──────────────────────────────────────────────┐
  │ OpenSearch Bulk Indexing                      │
  │  - Index: tax_authority_rag_chunks            │
  │  - HNSW: m=16, ef_construction=256            │
  │  - Engine: nmslib (cosinesimil)               │
  │  - Upsert by chunk_id (deterministic)         │
  │  - Shards: 6, replicas: 1                     │
  └──────────────────────────────────────────────┘
                │
                ▼
  ┌──────────────────────────────────────────────┐
  │ Cache Invalidation Callback                   │
  │  - For each re-indexed doc_id:                │
  │    semantic_cache.invalidate_by_doc_ids([id]) │
  │  - Prevents stale answers after amendments    │
  └──────────────────────────────────────────────┘
```

See [pseudocode/module1_ingestion.py](../pseudocode/module1_ingestion.py) for
the full `LegalDocumentChunker` and `IngestionPipeline` implementation.

---

## 5. Module-to-Diagram Index

| Module | Responsibility | Pseudo-code | Detail diagram |
|---|---|---|---|
| **Module 1** — Ingestion & Chunking | Structure-aware parsing, metadata inheritance, embedding, indexing | [module1_ingestion.py](../pseudocode/module1_ingestion.py) | Section 4 of this file |
| **Module 2** — Retrieval Strategy | Three-path hybrid search, RRF fusion, cross-encoder rerank | [module2_retrieval.py](../pseudocode/module2_retrieval.py) | [retrieval_flow.md](retrieval_flow.md) |
| **Module 3** — Agentic RAG (CRAG) | State machine, grading gate, retry, citation validation | [module3_crag_statemachine.py](../pseudocode/module3_crag_statemachine.py), [module3_grader.py](../pseudocode/module3_grader.py) | [crag_state_machine.md](crag_state_machine.md) |
| **Module 4** — Ops / Security / Eval | RBAC, cache, CI/CD gates, observability | [module4_cache.py](../pseudocode/module4_cache.py), [schemas/rbac_roles.json](../schemas/rbac_roles.json), [eval/metrics_matrix.md](../eval/metrics_matrix.md) | [security_model.md](security_model.md) |

---

## 6. TTFT Latency Budget (sums to 1500 ms)

Justification for Assumption A13. Each stage has a hard p95 target; the buffer
absorbs network jitter, GC pauses, and cross-AZ hops.

| # | Stage | Budget (p95) | Notes |
|---|---|---:|---|
| 1 | Cache check (embed + RediSearch KNN) | 15 ms | Redis in-memory; embedding shared with retrieval if cache miss |
| 2 | Query embedding (multilingual-e5-large, GPU) | 30 ms | Not re-paid if the cache step already embedded |
| 3 | Hybrid retrieval (BM25 ∥ kNN) | 80 ms | Parallel execution: max(BM25 ~20 ms, kNN ~80 ms) |
| 4 | Cross-encoder rerank (40 pairs, batched) | 200 ms | BAAI/bge-reranker-v2-m3 on GPU |
| 5 | CRAG grader (batched LLM call over 8 chunks) | 150 ms | Single prompt, 8 grades returned as JSON |
| 6 | LLM first token (generator) | 800 ms | Mixtral 8x22B vLLM or Azure OpenAI GPT-4 Gov |
| 7 | Buffer (network, serialization, jitter) | 225 ms | Headroom for p99 tail |
| — | **Total p95 TTFT (cache miss)** | **1500 ms** | Hard cap — anything over = page on-call |
| — | Total p95 TTFT (cache hit) | **~15 ms** | Skips steps 2–6 entirely |

**Retry scenario** (context graded AMBIGUOUS → rewrite → re-retrieve):
- Adds: query rewrite LLM call (~150 ms) + second retrieval (~80 ms) + second rerank (~200 ms) + second grading (~150 ms)
- Worst case: ~1500 ms + 580 ms + reduced buffer = **requires MAX_RETRIES = 1**
- With 2 retries the worst case exceeds 1500 ms, which is why the state machine hard-caps retries at 1.

See [pseudocode/module3_crag_statemachine.py:42-54](../pseudocode/module3_crag_statemachine.py) for the
in-code justification of `MAX_RETRIES = 1`.

---

## 7. What This Diagram Establishes

1. **Every online query is wrapped by the semantic cache first.** The CRAG state
   machine is not entered on a cache hit. This is where the "~15 ms TTFT for
   repeat queries" claim originates.
2. **RBAC is enforced inside OpenSearch, not in application code.** The DLS
   filter is applied BEFORE BM25 scoring and kNN distance computation — see
   [security_model.md](security_model.md) for the mathematical proof of why
   this matters.
3. **The ingestion pipeline is fully separated from the query path.** The only
   coupling is the cache-invalidation callback, which prevents stale answers
   after legal amendments.
4. **Three retrieval paths, not two.** Exact-ID lookup (ECLI / Article patterns)
   bypasses vector search entirely — see [retrieval_flow.md](retrieval_flow.md).
5. **The CRAG state machine has explicit refusal states.** Retrieval grading
   (gate 1) and citation validation (gate 2) can both route to REFUSE. See
   [crag_state_machine.md](crag_state_machine.md).
6. **Observability is a first-class concern, not a bolt-on.** Every node emits a
   span, every metric is in Prometheus, every query/response is audit-logged.
   Satisfies Assumption A18.
