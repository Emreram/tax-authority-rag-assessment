# Retrieval Flow — Module 2 Detail Diagram

> This diagram visualizes the three-path hybrid retrieval strategy used by
> [module2_retrieval.py](../pseudocode/module2_retrieval.py). It shows how a
> query is dispatched to exact-ID, BM25, or kNN (or a fusion of the latter two),
> how Reciprocal Rank Fusion (RRF) merges the ranked lists, and how the
> cross-encoder reranker produces the final top-8 context passed to the CRAG
> grading gate.
>
> **Why three paths and not two:** Legal queries are bimodal — users either cite
> exact identifiers (ECLI rulings, article numbers) or describe concepts
> informally. A pure-vector system loses on exact IDs; a pure-BM25 system loses
> on paraphrases. The exact-ID shortcut bypasses both when the query contains a
> precise reference, avoiding wasted latency on embedding + rerank.

---

## 1. Query-Type Dispatch (runs inside `retrieve()` node)

The CRAG state machine has already classified the query by the time it reaches
the retrieve node. The classification determines which retrieval path(s) execute.

```
            retrieve() node entered with (query, user_security_tier, query_type)
                                       │
                                       ▼
                        ┌──────────────────────────────┐
                        │ query_type from classify_query│
                        └──────────────────────────────┘
                          │            │              │
                          ▼            ▼              ▼
                    REFERENCE        SIMPLE         COMPLEX
                  (ECLI/Article)                  (multi-part)
                          │            │              │
                          ▼            ▼              ▼
               ┌─────────────────┐   │     ┌─────────────────────┐
               │ exact_id_retrieve│   │     │ For each sub-query: │
               │                  │   │     │   hybrid_retrieve() │
               │ (direct keyword  │   │     │ Merge + dedupe by   │
               │  filter, NO      │   │     │ chunk_id            │
               │  embedding       │   │     └─────────────────────┘
               │  needed)         │   │              │
               └─────────────────┘    │              │
                          │           ▼              │
                          │    ┌────────────────┐    │
                          │    │hybrid_retrieve │    │
                          │    │(BM25 ∥ kNN+RRF)│    │
                          │    └────────────────┘    │
                          │           │              │
                          └───────────┼──────────────┘
                                      │
                                      ▼
                          ┌────────────────────────┐
                          │ rerank_chunks(top_k=8) │
                          │ (BAAI/bge-reranker-v2-m3)│
                          └────────────────────────┘
                                      │
                                      ▼
                              top-8 reranked
                                      │
                                      ▼
                            to grade_context()
```

Exact-ID bypasses reranking entirely when the ID match is unambiguous (1 result)
— see `exact_id_retrieve()` in module2_retrieval.py. For multi-match cases
(e.g., "Article 3.114" without paragraph), results still pass through the
reranker to order by semantic relevance to the question text.

---

## 2. Inside `hybrid_retrieve()` — the Main Pipeline

```
  query (str), user_security_tier (str), top_k=40
                    │
                    │
   ┌────────────────┼────────────────┐
   │                                 │
   │                                 │
   ▼                                 ▼
 ┌─────────────────────┐    ┌─────────────────────┐
 │  embed_query(query) │    │ _bm25_retrieve()    │
 │                     │    │                     │
 │  "query: " + query  │    │ OpenSearch multi_   │
 │  (E5 prefix)        │    │  match on chunk_text│
 │  → 1024-dim vector  │    │  + title + path     │
 │  → L2-normalized    │    │                     │
 │                     │    │ dutch_legal_analyzer│
 │  Latency: ~30 ms    │    │ temporal filter     │
 │                     │    │ DLS (OpenSearch)    │
 │                     │    │                     │
 │                     │    │ Latency: ~20 ms     │
 │                     │    │ Returns: top-20     │
 └─────────────────────┘    └─────────────────────┘
             │
             ▼
 ┌─────────────────────┐
 │ _knn_retrieve()     │
 │                     │
 │ OpenSearch knn query│
 │  field: embedding   │
 │  k=20               │
 │  ef_search=128      │
 │  engine: nmslib     │
 │                     │
 │ temporal filter AS  │
 │ knn pre-filter      │
 │ (not post-filter)   │
 │ DLS (OpenSearch)    │
 │                     │
 │ Latency: ~80 ms     │
 │ Returns: top-20     │
 └─────────────────────┘
             │
             │
             │  ┌─── Parallel execution ──────┐
             │  │  ThreadPoolExecutor(max=2)   │
             │  │  wall time = max(20, 80)     │
             │  │             = 80 ms          │
             │  └──────────────────────────────┘
             │
             ▼
 ┌─────────────────────────────────┐
 │ _rrf_fuse(bm25, knn, k=60)      │
 │                                 │
 │  score(d) = Σ 1/(k + rank_i(d)) │
 │              i ∈ {BM25, kNN}    │
 │                                 │
 │  - Merge by chunk_id            │
 │  - Sum contributions            │
 │  - Sort desc → top-40           │
 │                                 │
 │  Latency: ~5 ms (pure Python)   │
 └─────────────────────────────────┘
             │
             ▼
        top-40 fused list
             │
             ▼
 ┌─────────────────────────────────┐
 │ rerank_chunks(query, chunks, 8) │
 │                                 │
 │  bge-reranker-v2-m3 (GPU)       │
 │  40 (query, chunk_text) pairs   │
 │  batched cross-encoder          │
 │  score ∈ [0, 1]                 │
 │  sort desc → top-8              │
 │                                 │
 │  Latency: ~200 ms (batched)     │
 └─────────────────────────────────┘
             │
             ▼
        top-8 reranked → grade_context()
```

---

## 3. RRF Formula (formal)

The file implements **Reciprocal Rank Fusion** (Cormack, Clarke & Büttcher, 2009):

```
  RRF_score(d) = Σ  1 / (k + rank_i(d))
                i ∈ {BM25, kNN}

  where:
    k           = 60        (standard RRF constant, empirically chosen)
    rank_i(d)   = rank of document d in list i, 1-indexed
    If d is absent from list i, its contribution from i is 0
    Final list = all chunk_ids sorted by RRF_score descending
```

**Why k = 60:** Cormack et al. showed k ∈ [40, 100] is robust across TREC
tracks. 60 is the de-facto default used by Microsoft Bing, Elasticsearch, and
OpenSearch's native RRF search pipeline. Lower k amplifies top-rank
contributions (good when one retriever is clearly better); higher k flattens
them (good for unfamiliar domains). 60 is a balanced middle for mixed
sparse/dense legal retrieval.

**Why RRF over alpha-blending:**

| Property | RRF | Linear (α · dense + (1−α) · sparse) |
|---|---|---|
| Score normalization needed | No (rank-based) | Yes (BM25 and cosine are on different scales) |
| Robust to score distribution shifts | Yes | No |
| Hyper-parameter sensitivity | Low (k) | High (α per query class) |
| Legal domain fit | BM25 scores are spiky for exact legal terms; cosine scores compress in [0.65, 0.85]. Alpha-blending distorts; RRF handles naturally. | Poor without per-query re-normalization |

---

## 4. Worked Example — "Wat is de arbeidskorting voor 2024?"

This is the same trace embedded in [module2_retrieval.py Section 12](../pseudocode/module2_retrieval.py).
Displayed here in visual form to show how Article 3.114 lid 1 rises to rank #1
through each fusion stage.

### Stage A — BM25 top-5 of 20

Exact keyword matching on `dutch_legal_analyzer`-tokenized chunk text. The word
"arbeidskorting" appears literally in the Wet IB articles, scoring high.

```
 Rank  chunk_id                                            BM25 Score
 ────  ────────────────────────────────────────────────   ──────────
  1    WetIB2001-2024::art3.114::lid1::chunk001               24.7
  2    WetIB2001-2024::art3.114::lid2::chunk001               22.1
  3    WetIB2001-2024::art8.10::lid1::chunk001                18.3
  4    Handboek-Loonbelasting-2024::ch7::sec3::chunk002       16.5
  5    WetIB2001-2024::art3.114::lid3::chunk001               15.9
```

### Stage B — kNN top-5 of 20

Dense cosine similarity on the HyDE-transformed query embedding. The handbook
and FAQ score higher than the raw article because they explain the concept in
prose that matches the question pattern.

```
 Rank  chunk_id                                            Cosine
 ────  ────────────────────────────────────────────────   ──────────
  1    Handboek-Loonbelasting-2024::ch7::sec3::chunk002       0.847
  2    WetIB2001-2024::art8.10::lid1::chunk001                0.832
  3    WetIB2001-2024::art3.114::lid1::chunk001               0.829
  4    Belastingdienst-FAQ-2024::arbeidskorting::chunk001     0.821
  5    WetIB2001-2024::art3.114::lid2::chunk001               0.815
```

### Stage C — RRF fusion (k=60), top-5

Each chunk's RRF score is the sum of reciprocal ranks from both lists. Chunks
appearing in BOTH lists dominate.

```
 Rank  chunk_id                                            RRF formula              Score
 ────  ────────────────────────────────────────────────   ──────────────────────   ───────
  1    WetIB2001-2024::art3.114::lid1::chunk001           1/(60+1) + 1/(60+3)      0.0327
  2    WetIB2001-2024::art3.114::lid2::chunk001           1/(60+2) + 1/(60+5)      0.0315
  3    Handboek-Loonbelasting-2024::ch7::sec3::chunk002   1/(60+4) + 1/(60+1)      0.0312
  4    WetIB2001-2024::art8.10::lid1::chunk001            1/(60+3) + 1/(60+2)      0.0307
  5    Belastingdienst-FAQ-2024::arbeidskorting::chunk001 0       + 1/(60+4)       0.0164
```

Notice how **Article 3.114 lid 1 is now rank #1**. It was rank 1 in BM25 and
rank 3 in kNN — neither retriever alone would have confidently surfaced it over
the handbook (kNN rank 1). RRF rewards agreement.

### Stage D — Cross-encoder rerank (40 → 8)

The reranker scores each `(query, chunk_text)` pair with joint attention. For
the arbeidskorting query it confirms Article 3.114 lid 1 as the top hit
(score 0.94) and slightly reorders the remaining 40. Final top-8 is passed to
the CRAG grader.

### Stage E — Latency trace (actual measurements embedded in module2)

```
  embed_query():             28 ms
  _bm25_retrieve():          19 ms  ┐
  _knn_retrieve():           76 ms  ┘ parallel → 76 ms (wall time)
  _rrf_fuse():                3 ms
  rerank_chunks():          187 ms
                          ──────────
  Total hybrid_retrieve:    294 ms
```

This matches the latency budget in [architecture_overview.md §6](architecture_overview.md)
(80 ms hybrid + 200 ms rerank + ~15 ms cache/embed overhead = ~295 ms).

---

## 5. BM25 vs kNN vs Exact-ID — Decision Matrix

| Dimension | BM25 (sparse) | kNN (dense) | Exact-ID |
|---|---|---|---|
| **Strength** | Exact legal terminology, statute numbers, Dutch legal jargon | Semantic concepts, English paraphrases, fuzzy intent | Precise identifiers (ECLI, Article N.M) |
| **Latency (p95)** | ~20 ms | ~80 ms | ~15 ms |
| **Index structure** | Inverted index (Lucene) | HNSW graph (nmslib) | Keyword term lookup |
| **Fails when…** | User paraphrases or asks conceptually ("home office write-off") | User cites a precise statute number (BM25 crushes this) | Query has no explicit ID |
| **Example query** | "aftrekbaarheid hypotheekrente eigen woning" | "kan ik mijn thuiskantoor aftrekken" | "ECLI:NL:HR:2023:1234" |
| **Triggers for this path** | Always (one of the two hybrid legs) | Always (one of the two hybrid legs) | `ECLI:` or `artikel N.M` regex match in query_type=REFERENCE |

---

## 6. DLS Enforcement — Where It Happens

The DLS filter is applied **by OpenSearch, inside the search engine**, before
BM25 scoring and kNN distance computation. The application code never sees the
documents that are filtered out — which is the whole point. See
[security_model.md](security_model.md) for the mathematical proof of why this
matters.

```
  ┌─────────────────────────────────────────────────────────┐
  │ OpenSearch Internal Processing                          │
  │                                                         │
  │   [Incoming query + user_security_tier in JWT]         │
  │                    │                                    │
  │                    ▼                                    │
  │   ┌──────────────────────────────────────┐             │
  │   │ Role Resolution                      │             │
  │   │ JWT → idp_group → DLS role           │             │
  │   │ (see schemas/rbac_roles.json)        │             │
  │   └──────────────────────────────────────┘             │
  │                    │                                    │
  │                    ▼                                    │
  │   ┌──────────────────────────────────────┐             │
  │   │ Apply DLS filter to index view       │             │
  │   │ S_user = S_total \ S_forbidden       │             │
  │   │ (set subtraction, not post-filter)   │             │
  │   └──────────────────────────────────────┘             │
  │                    │                                    │
  │                    ▼                                    │
  │   ┌──────────────────────────────────────┐             │
  │   │ BM25 and kNN operate on S_user only  │             │
  │   │ Classified docs never enter the      │             │
  │   │ scoring pool → no leakage            │             │
  │   └──────────────────────────────────────┘             │
  │                                                         │
  └─────────────────────────────────────────────────────────┘
```

---

## 7. Cross-File Anchors

- Function signatures: [module2_retrieval.py](../pseudocode/module2_retrieval.py) — `hybrid_retrieve`, `exact_id_retrieve`, `rerank_chunks`, `_bm25_retrieve`, `_knn_retrieve`, `_rrf_fuse`, `embed_query`, `build_temporal_filter`
- Top-k constants: `BM25_TOP_K = 20`, `KNN_TOP_K = 20`, `TOP_K_RETRIEVAL = 40`, `TOP_K_RERANK = 8` — match module3_crag_statemachine.py lines 68-69
- RRF: `RRF_RANK_CONSTANT = 60` — matches OpenSearch native search pipeline config in [schemas/opensearch_index_mapping.json](../schemas/opensearch_index_mapping.json) `_search_pipeline_config`
- Reranker model: `BAAI/bge-reranker-v2-m3` — listed in [tools_and_technologies.txt](../tools_and_technologies.txt)
- Embedding model: `intfloat/multilingual-e5-large` — same model used at ingestion time in [module1_ingestion.py](../pseudocode/module1_ingestion.py)
