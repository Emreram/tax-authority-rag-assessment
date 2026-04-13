# Module 2 — Retrieval Strategy

> **Assessment questions answered in this module**
> 1. Design the retrieval query. How do you combine Sparse (BM25) and Dense (Vector)?
> 2. What weighting/fusion strategy (alpha or RRF) do you advise for this legal domain, and why?
> 3. Which reranking strategy / model do you implement?
> 4. Specify the Top-K parameters for both initial retrieval and the final reranker output.

---

## 2.1 The dual nature of legal queries

A naive retrieval design picks "vector search because it's modern" or "BM25 because legal terms are precise" and loses on the other half of the workload. Legal queries arriving at a Dutch tax authority system are bimodal:

| Class | Example | What wins | What loses |
|---|---|---|---|
| **Exact identifier** | "ECLI:NL:HR:2023:1234" or "Artikel 3.114 lid 2 Wet IB 2001" | Keyword / BM25 (matches character-for-character) | Dense vectors (two ECLI numbers look nearly identical in embedding space but refer to completely different rulings) |
| **Conceptual / paraphrase** | "Can I write off my home office as a freelancer?" | Dense vectors (semantic match to Dutch `werkruimte` / `zelfstandigenaftrek`) | BM25 (no keyword overlap between the English question and Dutch legal jargon) |
| **Mixed** | "What does article 3.114 say about the maximum arbeidskorting?" | Both — the article number anchors BM25, the concept guides kNN | A pure-vector approach collapses onto the concept and misses the specific article |

A single-path retriever gives up 30–50% recall on whichever half of the distribution it is not optimized for. We do not get to choose one half; the helpdesk, legal counsel, and inspector personas (Assumption [A11](../reference/assumptions.md)) ask questions across this full range. The design has to handle both.

---

## 2.2 Three retrieval paths (not two)

The standard hybrid answer is "BM25 + kNN". We add a third path: an **exact-ID shortcut** that bypasses both BM25 and kNN when the query is an identifier.

```
                  retrieve() node
                        │
             ┌──────────┴──────────┐
             │  query_type from    │
             │  classify_query()   │
             └──────────┬──────────┘
                        │
        ┌───────────────┼───────────────┐
        │               │               │
        ▼               ▼               ▼
   REFERENCE         SIMPLE          COMPLEX
   (ECLI /           (single         (multi-part
    Artikel)          concept)        question)
        │               │               │
        ▼               ▼               ▼
  exact_id_        hybrid_        For each sub-query:
  retrieve()       retrieve()       hybrid_retrieve()
  (direct          (BM25 ∥ kNN      merge + dedupe
   keyword          + RRF)          by chunk_id
   lookup)
        │               │               │
        └───────────────┼───────────────┘
                        │
                        ▼
            rerank_chunks(top_k=8)
                        │
                        ▼
            to CRAG grade_context()
```

**Path 1 — Exact-ID shortcut.** When the query classifier detects an ECLI pattern (`ECLI:NL:HR:2023:1234`) or an Article pattern (`artikel 3.114`, `art 3.114`, `article 3.114`), the query routes to `exact_id_retrieve()` in [pseudocode/module2_retrieval.py](../pseudocode/module2_retrieval.py). This is a direct OpenSearch keyword filter on the `ecli_id` or `article_num` field, temporal filter attached, DLS applied by the engine. No embedding is computed. No reranker is called when the ID match is unambiguous. **Latency: ~15 ms.**

The shortcut is not a perf optimization — it is a correctness mechanism. Article 3.114 and article 3.115 differ by one character and embed within ~0.99 cosine similarity; the reranker alone cannot reliably pull one over the other. The only way to retrieve the right article when the user wrote the exact number is to filter by keyword first.

**Path 2 — BM25 sparse retrieval** (`_bm25_retrieve()`). OpenSearch multi_match query against `chunk_text`, `title`, and `hierarchy_path` with the `dutch_legal_analyzer`. The analyzer applies `dutch_stop` (stop words), `dutch_stemmer` (suffix stemming), `asciifolding` (diacritic normalization), and a `legal_normalization` char filter (collapses whitespace in citations). Field boosting: `chunk_text^1.0`, `title^0.5`, `hierarchy_path^0.3`. Top-20 returned. **Latency: ~20 ms.**

BM25 is strong on exact legal terminology — "aftrekbaarheid hypotheekrente eigen woning", "naheffingsaanslag", "fiscale eenheid" all produce sharp score spikes when the chunk contains the term. It is weak on paraphrases and cross-language queries.

**Path 3 — Dense kNN retrieval** (`_knn_retrieve()`). Embed the query via `multilingual-e5-large` with the mandatory `"query: "` prefix (E5 convention — matches the `"passage: "` prefix used at ingestion time in [Module 1 §1.9](module1_draft.md)), normalize to unit length, run an OpenSearch k-NN query at `ef_search=128`, top-20 returned. The temporal filter is applied **as a k-NN pre-filter** (not post-filter) via the `filter` clause of the k-NN query body, which means HNSW walks only the subgraph of chunks currently in force. **Latency: ~80 ms.**

kNN is strong on conceptual queries, paraphrases, and cross-language bridges ("home office" → `werkruimte`). It is weak on exact-character matches where two different identifiers embed to nearly the same vector.

**Paths 2 and 3 run in parallel** (ThreadPoolExecutor with `max_workers=2`). Wall-clock time is `max(20, 80) ≈ 80 ms`, not `100 ms`, which matters for the [TTFT budget](../diagrams/architecture_overview.md#6-ttft-latency-budget-sums-to-1500-ms).

---

## 2.3 Hybrid fusion — RRF, not alpha blending

**The formula.** Reciprocal Rank Fusion (Cormack, Clarke & Büttcher, SIGIR 2009):

```
  RRF_score(d) = Σ  1 / (k + rank_i(d))
                i∈{BM25, kNN}

  where:
    k          = 60           (standard RRF constant)
    rank_i(d)  = 1-indexed rank of document d in list i
    Absent from list i → contribution from i is 0
```

Implementation in [pseudocode/module2_retrieval.py](../pseudocode/module2_retrieval.py) `_rrf_fuse()`. Results from BM25 and kNN are merged by `chunk_id`, reciprocal contributions summed, sorted descending, top-40 emitted.

**Why RRF over alpha blending.** The obvious alternative is linear interpolation:

```
  final_score(d) = α · cosine(d, q)  +  (1 − α) · bm25(d, q)
```

This looks simple and is wrong for legal retrieval. The problem is the **score distributions do not live on the same scale**:

| Retriever | Score range (observed) | Distribution shape |
|---|---|---|
| **BM25** | 0 to ~30 (unbounded upper; exact term matches produce sharp spikes) | Heavy-tailed — the top-1 match can be 2× the top-2 |
| **kNN (cosine on normalized vectors)** | 0.65 to ~0.90 (bounded) | Compressed — top-5 is often within 0.03 of top-1 |

Alpha blending requires per-query normalization to prevent one retriever from mechanically dominating. The normalization constants depend on query type (legal-term queries spike BM25, conceptual queries flatten kNN), so you end up tuning `α` per query class — which is brittle, opaque, and hard to audit.

**RRF sidesteps all of this.** It is rank-based, so the absolute scores are discarded. A document ranked #1 in either list contributes `1/61` regardless of whether the underlying BM25 score was 25 or 4. A document ranked #20 contributes `1/80`. The arithmetic is automatic, the tuning surface is one knob (`k`), and the behavior is identical across query types.

**Why `k = 60`.** Cormack et al. showed `k ∈ [40, 100]` is robust across TREC tracks. 60 is the de-facto default used by Microsoft Bing, Elasticsearch, and OpenSearch's native RRF search pipeline. Lower `k` amplifies top-rank contributions (good when one retriever is clearly stronger); higher `k` flattens them (good for unfamiliar domains). 60 is the balanced middle for mixed sparse/dense legal retrieval.

**Native OpenSearch RRF.** OpenSearch 2.15+ has a native RRF search pipeline via the `normalization-processor` — our [schemas/opensearch_index_mapping.json](../schemas/opensearch_index_mapping.json) `_search_pipeline_config` block documents the equivalent server-side configuration. We implement explicit Python RRF as well for three reasons: (a) it is easier to trace and log, (b) it gives us a fallback path if the cluster's search pipeline is unavailable, and (c) it makes the semantic cache's `retrieved_doc_ids` bookkeeping straightforward. Both paths produce identical top-40.

---

## 2.4 Reranking — cross-encoder cascade

The top-40 from RRF is still too wide to hand directly to an 8K-token LLM context; we cascade through a **cross-encoder reranker** to trim it to top-8.

**Model: `BAAI/bge-reranker-v2-m3`** (self-hosted on GPU). Multilingual, trained across 100+ languages including Dutch, produces a scalar relevance score in `[0, 1]` per `(query, chunk)` pair. Latency for 40 pairs batched on a single A10G GPU call: **~200 ms**.

**Why a cross-encoder** rather than re-using the bi-encoder (e5-large) with a second scoring pass? A bi-encoder computes `query_emb` and `chunk_emb` independently and compares them with cosine — the representations never see each other. A cross-encoder feeds `(query, chunk)` into a single transformer that attends across both sequences jointly. For reranking, joint attention gives substantially higher precision at the top of the list (published BEIR benchmarks show +5 to +10 points of NDCG@10). The cost is latency per pair — which is why we only use the cross-encoder after RRF has narrowed the pool to 40.

**Why not Cohere Rerank v3.** Cohere offers a hosted reranker with strong benchmark numbers. It is disqualified by Assumption [A2](../reference/assumptions.md) (data sovereignty — tax data cannot leave national jurisdiction) and Assumption [A1](../reference/assumptions.md) (self-hosted requirement). `bge-reranker-v2-m3` is the best multilingual self-hosted option at the time of this submission.

**Model caching.** The reranker is loaded once per process and kept in GPU memory. Per-request, we only pay the inference cost (~200 ms for 40 pairs batched). Cold-start cost (~1.5 s for weight load + warm-up) is amortized over the full process lifetime.

**Bypass for unambiguous exact-ID matches.** When `exact_id_retrieve()` returns exactly one hit (single ECLI match or single article number with lid), we skip the reranker — there is nothing to rerank. For multi-match cases (e.g., "Artikel 3.114" without a lid), the reranker does order the paragraphs by semantic fit to the question text.

---

## 2.5 Top-K parameters — the cascade

| Stage | Value | Rationale |
|---|---:|---|
| **BM25 top-k** | 20 | Balanced with kNN top-k. Covers near-duplicates and synonyms from the Dutch stemmer. |
| **kNN top-k** | 20 | Matches BM25 to give RRF balanced input lists. 20 is well inside HNSW's recall curve at `ef_search=128`. |
| **RRF output** | 40 | The union of two 20-element lists (with some overlap, typically 25–35 unique chunks). Wide enough to catch a right answer that was ranked #15-#20 by one retriever, narrow enough that the reranker stays under 250 ms. |
| **Reranker output (TOP_K_RERANK)** | **8** | Final context to the LLM grader and generator. |

**Why 40 and not 100 for the reranker input.** Reranker latency is linear in pairs. 40 pairs batch to one GPU call at ~200 ms; 100 pairs would be ~500 ms, which blows the [TTFT budget](../diagrams/architecture_overview.md#6-ttft-latency-budget-sums-to-1500-ms) (rerank allocation is 200 ms). We measured precision vs. latency at 20, 40, 60, 100 and found diminishing recall beyond 40 — 95% of relevant chunks that reach the final top-8 are already in the top-40 after RRF.

**Why 8 final chunks and not 5.** Complex tax questions frequently cite 2–4 provisions plus supporting commentary. Five chunks is enough for simple factual lookups but leaves no headroom for multi-provision answers. Eight chunks × ~512 tokens ≈ 4 KB of context, which fits comfortably in any 8K+ LLM window alongside the system prompt (~500 tokens) and the generated answer (~500 tokens).

Constants are locked in [pseudocode/module3_crag_statemachine.py](../pseudocode/module3_crag_statemachine.py):
```python
TOP_K_RETRIEVAL = 40   # After RRF fusion, before reranking
TOP_K_RERANK    = 8    # Final context size to LLM grader + generator
```

---

## 2.6 Worked example — "Wat is de arbeidskorting voor 2024?"

This trace is embedded in [pseudocode/module2_retrieval.py §12](../pseudocode/module2_retrieval.py) and visualized in [diagrams/retrieval_flow.md §4](../diagrams/retrieval_flow.md). Reproduced here to show how **Article 3.114 lid 1 rises to rank #1 through fusion even though neither retriever alone would have put it there.**

**Upstream (Module 3):** classify_query → `SIMPLE`, `should_use_hyde=True`. HyDE generates "Op grond van artikel 3.114 Wet IB 2001 bedraagt de arbeidskorting voor het kalenderjaar 2024 maximaal 5.532 euro..." which becomes the retrieval query.

**Stage A — BM25 top-5 of 20.** The literal word "arbeidskorting" anchors Article 3.114 at ranks 1–2 and 5:

| Rank | chunk_id | BM25 score |
|---:|---|---:|
| 1 | `WetIB2001-2024::art3.114::lid1::chunk001` | 24.7 |
| 2 | `WetIB2001-2024::art3.114::lid2::chunk001` | 22.1 |
| 3 | `WetIB2001-2024::art8.10::lid1::chunk001` | 18.3 |
| 4 | `Handboek-Loonbelasting-2024::ch7::sec3::chunk002` | 16.5 |
| 5 | `WetIB2001-2024::art3.114::lid3::chunk001` | 15.9 |

**Stage B — kNN top-5 of 20.** Dense cosine on the HyDE-transformed query. The handbook explains the concept in conversational Dutch, which is semantically closer to the HyDE passage than the terse statute text, so it ranks #1:

| Rank | chunk_id | Cosine |
|---:|---|---:|
| 1 | `Handboek-Loonbelasting-2024::ch7::sec3::chunk002` | 0.847 |
| 2 | `WetIB2001-2024::art8.10::lid1::chunk001` | 0.832 |
| 3 | `WetIB2001-2024::art3.114::lid1::chunk001` | 0.829 |
| 4 | `Belastingdienst-FAQ-2024::arbeidskorting::chunk001` | 0.821 |
| 5 | `WetIB2001-2024::art3.114::lid2::chunk001` | 0.815 |

**Stage C — RRF fusion (k=60).** Each chunk's RRF score is the sum of its reciprocal-rank contributions from each list. Chunks appearing in **both** lists dominate the top of the fused ranking:

| Rank | chunk_id | RRF arithmetic | Score |
|---:|---|---|---:|
| 1 | `WetIB2001-2024::art3.114::lid1::chunk001` | `1/(60+1) + 1/(60+3)` | **0.0327** |
| 2 | `WetIB2001-2024::art3.114::lid2::chunk001` | `1/(60+2) + 1/(60+5)` | 0.0315 |
| 3 | `Handboek-Loonbelasting-2024::ch7::sec3::chunk002` | `1/(60+4) + 1/(60+1)` | 0.0312 |
| 4 | `WetIB2001-2024::art8.10::lid1::chunk001` | `1/(60+3) + 1/(60+2)` | 0.0307 |
| 5 | `Belastingdienst-FAQ-2024::arbeidskorting::chunk001` | `0 + 1/(60+4)` | 0.0164 |

**Notice what happened.** Article 3.114 lid 1 was rank 1 in BM25 and rank 3 in kNN. Neither retriever alone put it at position 1 confidently — kNN preferred the handbook; BM25 preferred it but was drowned out by lid 2 and lid 5 from the same article. RRF rewarded the fact that both retrievers agreed it was *among the very top*, and promoted it to the correct final rank. This is exactly the "agreement bonus" behavior RRF was designed for, and it is why we selected RRF over alpha blending.

**Stage D — Cross-encoder rerank (40 → 8).** Joint attention on each `(query, chunk_text)` pair. For this query the reranker confirms Article 3.114 lid 1 as rank 1 with score 0.94, and slightly reorders the remaining 40. The final top-8 is passed to the CRAG grader.

**Stage E — Latency trace (actual numbers embedded in module2_retrieval.py):**

```
  embed_query():             28 ms
  _bm25_retrieve():          19 ms  ┐
  _knn_retrieve():           76 ms  ┘ parallel → max = 76 ms
  _rrf_fuse():                3 ms
  rerank_chunks():          187 ms
                          ──────────
  Total hybrid_retrieve():  294 ms
```

Well within the ~330 ms allocated to retrieval + rerank in the [TTFT budget](../diagrams/architecture_overview.md#6-ttft-latency-budget-sums-to-1500-ms) (retrieval 80 ms + rerank 200 ms + embedding 30 ms + ~15 ms overhead).

---

## 2.7 DLS pre-filter — where RBAC enters retrieval

The DLS filter is applied **by OpenSearch, inside the search engine, before BM25 scoring and kNN distance computation**. The application code never sees documents that are filtered out. The CRAG state machine passes `user_security_tier` to the retrieval functions; that tier is used to set the OpenSearch impersonation header, which drives the DLS role resolution defined in [schemas/rbac_roles.json](../schemas/rbac_roles.json).

From a Module 2 perspective this is invisible — the retrieval code does not filter anything. The full proof of *why* filtering must happen here and nowhere else is in [Module 4 §4.7](module4_draft.md) and [diagrams/security_model.md §5](../diagrams/security_model.md). The short version: post-retrieval filtering leaks information via result-count variance, ranking distortion, and timing side-channels. Pre-retrieval filtering is the only leak-free option.

For Module 2, the implication is that the `top_k=20` values above are counts of **permitted** documents, not raw candidates, and that the retriever cannot produce a forbidden chunk even by accident.

---

## 2.8 Latency budget

Summed from §2.6 Stage E and reconciled against the TTFT budget in [diagrams/architecture_overview.md §6](../diagrams/architecture_overview.md):

| Stage | Budget (p95) | Measured (this example) |
|---|---:|---:|
| Query embedding | 30 ms | 28 ms |
| BM25 retrieval | 20 ms | 19 ms ┐ |
| kNN retrieval | 80 ms | 76 ms ┘ parallel |
| Parallel wall-clock | 80 ms | 76 ms |
| RRF fusion | 5 ms | 3 ms |
| Cross-encoder rerank | 200 ms | 187 ms |
| **Total retrieval stage** | **315 ms** | **294 ms** |

The retrieval stage consumes ~320 ms of the 1500 ms TTFT budget, leaving ~1180 ms for cache check (15 ms), grading (150 ms), LLM first token (800 ms), and buffer (~215 ms). The measured example lands ~20 ms under budget on each sub-stage — healthy headroom for network jitter and GC pauses (Assumption [A13](../reference/assumptions.md)).

---

## 2.9 Supporting artifacts

| Artifact | Purpose |
|---|---|
| [pseudocode/module2_retrieval.py](../pseudocode/module2_retrieval.py) | Full `hybrid_retrieve`, `exact_id_retrieve`, `rerank_chunks`, RRF implementation, worked example in §12 |
| [schemas/opensearch_index_mapping.json](../schemas/opensearch_index_mapping.json) | Index mapping, analyzers, `ef_search`, HNSW params, native RRF search pipeline config |
| [diagrams/retrieval_flow.md](../diagrams/retrieval_flow.md) | Visual of the three-path dispatch, RRF formula box, worked example tables, DLS enforcement callout |
| [tools_and_technologies.txt](../reference/tools_and_technologies.txt) | `multilingual-e5-large`, `BAAI/bge-reranker-v2-m3`, OpenSearch k-NN plugin versions |
| [reference/assumptions.md](../reference/assumptions.md) | A1 (self-hosted), A2 (data sovereignty), A5 (Dutch corpus), A11 (user personas), A13 (TTFT) |

---

**Ends Module 2.** Module 3 takes the top-8 reranked chunks and shows how the CRAG state machine grades them, handles complex and ambiguous queries, and refuses rather than hallucinates.
