# Enterprise RAG Architecture — Dutch Tax Authority

**Technical Assessment Response**
**Role**: Lead AI Engineer
**Date**: 2026-04-12
**Primary stack**: OpenSearch 2.15+ / LlamaIndex / LangGraph / Redis Stack / self-hosted models

Supporting artifacts: [pseudocode/](../pseudocode/) · [schemas/](../schemas/) · [diagrams/](../diagrams/) · [prompts/](../prompts/) · [eval/](../eval/) · [requirements.txt](../requirements.txt) · [.github/workflows/eval_gate.yml](../.github/workflows/eval_gate.yml)

---

> ## ⚠️ Lees dit eerst — verschil tussen dit document en de live demo
>
> Dit document beschrijft het **productie-ontwerp** voor een 20M-chunks corpus op een 3-node OpenSearch-cluster met GPU-LLM. De **live demo** in [`demo/`](../demo/) is een gereduceerde implementatie die op een normale laptop draait. De architectonische keuzes (RRF k=60, pre-retrieval RBAC, MAX_RETRIES=1, CRAG-grading, parent-expansion, semantic cache) zijn in beide identiek; de stack-keuzes verschillen op drie punten:
>
> | Onderdeel | Dit ontwerp (productie) | Live demo (laptop) | Reden |
> |---|---|---|---|
> | Orchestrator | LangGraph 9-state machine | Imperatieve Python-state machine | LangGraph voegt complexiteit toe zonder winst bij 9 states; imperatief is beter te auditen voor een toezichthouder |
> | LLM | Mixtral 8x22B / Llama 3.1 70B via vLLM | `ai/gemma4:E2B` via Docker Model Runner | Productie heeft GPU-cluster; laptop heeft CPU + 8 GB RAM |
> | Embeddings | `multilingual-e5-large` (1024-dim) + `bge-reranker-v2-m3` cross-encoder | `multilingual-e5-small` (384-dim) + LLM-as-reranker (zelfde Gemma-call) | Kleinere modellen passen op laptop; reranker-rol vervalt naar de Gemma-pass |
>
> Alle andere aspecten van dit document (HNSW-tuning, RBAC-model, CRAG-states, eval-gate, prompts, chunkstrategie, hiërarchische metadata) zijn 1-op-1 wat de demo doet. Voor een schoon overzicht van wat in deze repo v1-design is en wat v3-implementatie, zie [`OUTDATED_AUDIT.md`](../OUTDATED_AUDIT.md).

---

## Executive Summary

A Retrieval-Augmented Generation platform for the Dutch National Tax Authority. Three user personas (tax inspectors, legal counsel, helpdesk staff), ~500,000 legal documents, ~20 million chunks, hard **TTFT p95 < 1500 ms**, fail-closed hallucination prevention, and strict RBAC with a CLASSIFIED_FIOD tier that helpdesk users must never reach.

The ten non-negotiable decisions:

1. **OpenSearch 2.15+ with k-NN plugin + BM25 + Document-Level Security** as the unified search backend. Data sovereignty (A2) forbids SaaS vector databases. OpenSearch is the only mature self-hostable system that unifies dense retrieval, sparse retrieval, and row-level access control in a single query engine.
2. **Structure-aware chunking on Dutch legal boundaries** (Wet → Hoofdstuk → Afdeling → Artikel → Lid). [`LegalDocumentChunker`](../pseudocode/module1_ingestion.py) splits only on structural boundaries, never mid-article, and propagates parent metadata to every child chunk — the only way to satisfy the exact-citation requirement in A12.
3. **HNSW m=16, ef_construction=256, ef_search=128** with **fp16 quantization** (primary: ~61 GB total including HNSW graph; SQ8 fallback: ~31 GB at ~1–2% recall loss). Fits a 3-node cluster with 32 GB RAM each.
4. **Three-path retrieval (exact-ID / BM25 / kNN) fused with RRF (k=60)**. RRF is rank-based and robust to BM25/cosine score distribution mismatch. Alpha blending would require per-query re-normalization. Top-20 + top-20 → 40 fused → cross-encoder rerank → top-8 for the LLM.
5. **`BAAI/bge-reranker-v2-m3`** as the cross-encoder reranker. Multilingual, self-hosted, strong on Dutch legal text. Rejected Cohere Rerank v3 on data-sovereignty grounds (A2).
6. **CRAG state machine in LangGraph** with 9 states, 2 conditional routers, and an explicit REFUSE state. A linear chain has no gate between retrieval and generation. This design has two: a grading gate and a citation-validation gate. Either can route to REFUSE.
7. **`MAX_RETRIES = 1`** on ambiguous retrieval. Happy path: ~1450 ms ✓. One retry adds ~580 ms → worst case ~2030 ms (over hard limit, but expected TTFT at ~15% retry probability stays under 1500 ms). Two retries → worst case ~2610 ms (expected TTFT also over budget — not acceptable).
8. **Semantic cache cosine threshold ≥ 0.97**, tag-partitioned by security tier. "Box 1 tarief 2024" vs "Box 1 tarief 2023": cosine ≈ 0.94 under multilingual-e5-large. A 0.90 threshold serves last year's tax rate for this year's question. 0.97 excludes the year-confusion case.
9. **Pre-retrieval DLS enforcement**, not post-retrieval filtering. Under post-filtering with k=40 and classified fraction 5%: `P(c ≥ 1) = 1 − 0.95^40 ≈ 0.87`. The user observes fewer than k results and infers classified content exists. Pre-retrieval filtering restricts the search space before scoring; the algorithm never sees forbidden documents.
10. **Ragas + DeepEval in a 4-stage CI/CD gate** (PR → Staging → Canary → Production). Blocking thresholds: Faithfulness ≥ 0.90, Context Precision@8 ≥ 0.85, Citation Accuracy = 1.0 (binary), DLS Bypass Rate = 0.0 (absolute). CI workflow: [.github/workflows/eval_gate.yml](../.github/workflows/eval_gate.yml). Sample test set: [eval/golden_test_set_spec.json](../eval/golden_test_set_spec.json).

---

## Explicit Assumptions

Each assumption is load-bearing. If it is wrong, the relevant decision changes.

### Deployment & Infrastructure

| # | Assumption | Architectural impact |
|---|---|---|
| **A1** | Deployment target is government cloud (Azure Government NL, AWS GovCloud, or on-premises). No SaaS for classified data. | All components self-hostable. Eliminates Pinecone, Weaviate Cloud, Cohere API. |
| **A2** | No data may leave national jurisdiction (EU/NL sovereignty, GDPR Art. 44+). | Embedding, reranking, and LLM inference run on-premises or in a gov-approved region. No US API calls. |
| **A3** | GPU infrastructure available: min 4× NVIDIA A100 80 GB for LLM, 2× A10G for embeddings + reranker. | Enables self-hosted Mixtral 8x22B / Llama 3.1 70B. Fallback: Azure OpenAI Government Cloud. |
| **A4** | Existing Identity Provider (AD / ADFS / Azure AD with OIDC). | RBAC maps IdP groups → OpenSearch DLS roles. JWT/OIDC at the API gateway. |

### Corpus & Language

| # | Assumption | Architectural impact |
|---|---|---|
| **A5** | Primary corpus is Dutch, with some English EU regulations and CJEU case law. | `multilingual-e5-large` for embeddings. BM25 uses `dutch_legal_analyzer`. |
| **A6** | 500,000 documents × ~40 chunks = ~20 million total chunks. | Drives HNSW m=16, fp16 quantization (~61 GB primary / SQ8 ~31 GB fallback), 6 shards. |
| **A7** | Batch ingestion (nightly or on-change). | Deterministic chunk IDs enable upsert on re-index. No streaming architecture. |
| **A8** | Standard Dutch legal structure: Wet → Hoofdstuk → Afdeling → Artikel → Lid → Sub. | Structure-aware chunker targets these boundaries with Dutch-specific regex. |
| **A9** | Case law uses ECLI identifiers: `ECLI:NL:{court}:{year}:{number}`. | ECLI queries bypass vector search via direct keyword filtering. |

### Users & Scale

| # | Assumption | Architectural impact |
|---|---|---|
| **A10** | Hundreds of concurrent users, peak ~200–500. | Single 3–5 node OpenSearch cluster. |
| **A11** | Three personas: Tax Inspectors (RESTRICTED), Legal Counsel (RESTRICTED), Helpdesk (PUBLIC + INTERNAL only). | Helpdesk benefits most from cache. Inspectors need decomposition. Legal counsel needs ECLI exact-match. |
| **A12** | Users expect exact citations: "Article X, Paragraph Y of [Law]" or "ECLI:NL:HR:2023:1234". | Citations reconstructed from chunk metadata, not generated freely. |

### System Requirements & Design Philosophy

| # | Assumption | Architectural impact |
|---|---|---|
| **A13** | TTFT < 1500 ms at p95 (hard requirement). | MAX_RETRIES = 1 is a mathematical consequence. |
| **A14** | Never fabricate citations or invent provisions. System MAY refuse rather than risk inaccuracy. | Fail-closed CRAG. Every gate failure → refuse. |
| **A15** | Assessment expects conceptual architecture + pseudo-code, not production codebase. | Focus on framework-aware clarity (LlamaIndex, LangGraph). |
| **A16** | Prefer false negatives over false positives. | High bar for RELEVANT. Conservative cache threshold. Refusal on citation failure. |
| **A17** | Security is a first-class concern. FIOD example signals the evaluator will check access control. | Pre-retrieval DLS non-negotiable. Cache is role-partitioned. |
| **A18** | The system will be audited. | OpenTelemetry traces + OpenSearch audit log + structured JSON everywhere. |

---

## Quick Answers — Module-by-Module

**Module 1 — Ingestion**
`LegalDocumentChunker` splits only on Dutch legal hierarchy boundaries (Wet → Hoofdstuk → Artikel → Lid), never mid-article. 22-field metadata schema; every chunk carries `article_num`, `paragraph_num`, `hierarchy_path`. Citations are reconstructed from metadata, not generated freely.
Vector DB: OpenSearch 2.15+. HNSW m=16, ef_construction=256, ef_search=128. fp16 quantization (~61 GB total incl. HNSW graph); SQ8 fallback (~31 GB, ~1–2% recall loss). 6 shards, 1 replica.

**Module 2 — Retrieval**
Three paths: exact-ID shortcut (ECLI/Article regex) / BM25 sparse top-20 / kNN dense top-20. Fusion: RRF k=60 (rank-based; robust to BM25/cosine score distribution mismatch). Reranker: BAAI/bge-reranker-v2-m3, self-hosted. Pipeline: 20 + 20 → 40 fused → 8 reranked → grader.

**Module 3 — Self-Healing CRAG**
LangGraph StateGraph, 9 states, 2 conditional routers, explicit REFUSE state. Grader gates between retrieval and generation. Citation validator gates between generation and response.
RELEVANT → generate · AMBIGUOUS + retry<1 → rewrite + retrieve · AMBIGUOUS retries exhausted / IRRELEVANT → refuse.
MAX_RETRIES=1: happy path ~1450 ms; worst case with 1 retry ~2030 ms (expected TTFT ≤1500 ms at ~15% retry probability).

**Module 4 — Production, Security, Evaluation**
Cache: Redis Stack · cosine ≥ 0.97 · tier-partitioned TAG pre-filter · TTL 0s (case law) / 24h (default) / 7d (procedural).
RBAC: pre-retrieval DLS. P(info leak under post-filter) = 1−0.95^40 ≈ 0.87.
Eval gate: Faithfulness ≥ 0.90 · Context Precision@8 ≥ 0.85 · Citation Accuracy = 1.0 · DLS Bypass Rate = 0.0. 4-stage CI/CD: PR → Staging → Canary (5%/2h) → Production.

---

## Architecture Overview

### High-level data flow (online path)

```
                      User (browser / CLI / IDE plugin)
                                    │  query + JWT (OIDC from AD/ADFS)
                                    ▼
                  ┌─────────────────────────────────────────┐
                  │  API Gateway (FastAPI + async)           │
                  │   validate JWT, extract security_tier    │
                  │   rate limit, audit log                  │
                  └─────────────────────────────────────────┘
                                    │
                                    ▼
                  ┌─────────────────────────────────────────┐
                  │  Semantic Cache (Redis Stack + RediSearch)│
                  │   embed query, KNN in tier-partitioned   │
                  │   index, threshold cos ≥ 0.97            │
                  └─────────────────────────────────────────┘
                        │                           │
                    HIT │ (~15 ms)              MISS│
                        ▼                           ▼
                    RESPOND              ┌──────────────────────────┐
                    (cached)             │  CRAG State Machine      │
                                         │  (LangGraph StateGraph)  │
                                         │  RECEIVE_QUERY            │
                                         │      ↓ classify_query()   │
                                         │  TRANSFORM_QUERY          │
                                         │      ↓ HyDE / decompose   │
                                         │  RETRIEVE  ◄──────┐       │
                                         │      ↓            │       │
                                         │  GRADE_CONTEXT    │ retry │
                                         │      ↓            │ ≤ 1   │
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
                                                    │ uses
                                                    ▼
                  ┌─────────────────────────────────────────┐
                  │  Hybrid Retrieval (OpenSearch 2.15+)     │
                  │   DLS pre-filter → S_user = S \ S_banned │
                  │   BM25 top-20 ∥ kNN top-20              │
                  │   RRF k=60 → top-40                     │
                  │   BAAI/bge-reranker-v2-m3 → top-8       │
                  └─────────────────────────────────────────┘
```

### Component grid

| Stage | Component | Technology | Module |
|---|---|---|---|
| Ingress | API Gateway | FastAPI + Uvicorn | 4 |
| Auth | Identity Provider | AD / ADFS / Azure AD (OIDC) | 4 |
| Cache | Semantic Cache | Redis Stack + RediSearch HNSW | 4 |
| Orchestration | CRAG Orchestrator | LangGraph StateGraph | 3 |
| Query transform | HyDE / Decomposer | LLM at T=0.3 | 3 |
| Retrieval | Hybrid Retriever | OpenSearch k-NN + BM25 | 2 |
| Retrieval | Embedding Service | multilingual-e5-large (GPU) | 1, 2 |
| Retrieval | Reranker | BAAI/bge-reranker-v2-m3 (GPU) | 2 |
| Safety gate | Retrieval Grader | batched LLM call | 3 |
| Generation | Generator LLM | Mixtral 8x22B vLLM or Azure OpenAI GPT-4 Gov | 3 |
| Safety gate | Citation Validator | Python set-membership | 3 |
| Storage | Chunk Index | OpenSearch (HNSW m=16, fp16) | 1, 4 |
| Storage | Audit Log | OpenSearch (separate index) | 4 |
| Observability | Tracing / Metrics / LLM logs | OpenTelemetry + Jaeger + Prometheus + LangSmith | 4 |
| Evaluation | Eval pipeline | Ragas + DeepEval + pytest | 4 |

### TTFT latency budget (sums to 1500 ms)

| # | Stage | p95 budget | Notes |
|---|---|---:|---|
| 1 | Cache check | 15 ms | Redis in-memory |
| 2 | Query embedding | 30 ms | Not re-paid on cache miss (shared) |
| 3 | Hybrid retrieval (BM25 ∥ kNN) | 80 ms | Parallel: max(~20 ms, ~80 ms) |
| 4 | Cross-encoder rerank (40 pairs) | 200 ms | bge-reranker-v2-m3 on GPU |
| 5 | Grader (8 chunks, batched LLM) | 150 ms | One prompt, JSON response |
| 6 | LLM first token | 800 ms | Mixtral 8x22B vLLM |
| 7 | Buffer (network, jitter) | 225 ms | Headroom for p99 tail |
| | **Total (cache miss)** | **1500 ms** | Hard cap |
| | Total (cache hit) | ~15 ms | |

**One retry** adds ~580 ms (rewrite 150 + retrieval 80 + rerank 200 + grader 150) → worst case ~2030 ms. Rare enough (~15% retry probability) that expected TTFT stays under 1500 ms. Two retries → ~2610 ms worst case and expected TTFT also exceeds budget. `MAX_RETRIES = 1` is a mathematical constraint, not a preference.

---

> **Supplementary — beyond this assessment:**
> [performance/resource_allocation.md](../performance/resource_allocation.md) covers GPU VRAM budgets per model, Redis sizing with eviction policy, QPS throughput modeling (bottleneck: reranker GPU at ~6 QPS), horizontal scaling triggers, cost per query (~$0.001 self-hosted vs ~$0.21 Azure OpenAI GPT-4), per-stage timeout and circuit breaker policy, ingestion throughput and full re-index SLA (~5.5 days for 20M chunks), and 7 production monitoring dashboards. This is not required by the assessment — it shows what a production-readiness review would look like.

---

# Module 1 — Ingestion & Knowledge Structuring

> Pseudo-code: [module1_ingestion.py](../pseudocode/module1_ingestion.py) · Schema: [chunk_metadata.json](../schemas/chunk_metadata.json) · [opensearch_index_mapping.json](../schemas/opensearch_index_mapping.json)

## 1.1 Why recursive text splitters fail for legal documents

`RecursiveCharacterTextSplitter(chunk_size=512)` splits at character counts. Applied to Wet IB 2001 it produces:

```
  ... de arbeidskorting bedraagt ingevolge artikel
  [CHUNK BOUNDARY]
  3.114, eerste lid, voor het kalenderjaar 2024 ...
```

The chunk containing "5.532 euro" no longer contains "art. 3.114, eerste lid". The LLM either refuses to cite (good, but no answer) or fabricates a plausible article number (bad — a fabricated legal citation in a tax authority system). Both violate A12 and A14. Structure-aware chunking treats the legal hierarchy as the primary splitting signal.

## 1.2 Chunking strategy — structure-aware parsing

Dutch legal hierarchy (A8):

```
  Wet → Hoofdstuk → Afdeling → Artikel → Lid → Sub
```

`LegalDocumentChunker` emits one chunk per leaf node. Boundary rules by document type:

| Document type | Primary boundary | Secondary |
|---|---|---|
| LEGISLATION (Wet IB, AWR, Wet OB) | Artikel / Lid | Sub-paragraph |
| CASE_LAW (ECLI rulings) | Overweging (consideration) | Paragraph within consideration |
| POLICY (Handboeken, Besluiten) | Numbered section heading | Paragraph |
| ELEARNING | H2/H3 headings | Paragraph |

Target: **256–512 tokens per chunk, zero overlap** on structural boundaries. A chunk either *is* Article 3.114 lid 2 or it is not. When an Artikel exceeds 512 tokens, split on sub-paragraphs and preserve parent metadata on every child via LlamaIndex `NodeRelationship.PARENT`.

## 1.3 Metadata schema — 22 fields

The [full schema](../schemas/chunk_metadata.json) defines 22 fields. The load-bearing ones for citation reconstruction:

| Field | Type | Example | Purpose |
|---|---|---|---|
| `chunk_id` | string | `WetIB2001-2024::art3.114::lid1::chunk001` | Deterministic, idempotent on re-index |
| `doc_id` | string | `WetIB2001-2024` | Document-level identifier |
| `doc_type` | enum | `LEGISLATION` | Drives boundary rules + retrieval path |
| `title` | string | `Wet inkomstenbelasting 2001` | Human-readable document title |
| `article_num` | string | `3.114` | Which article — nullable for non-article docs |
| `paragraph_num` | string | `1` | Which paragraph — nullable |
| `sub_paragraph` | string | `a` | Which sub — nullable |
| `chapter` | string | `3` | Parent chapter number |
| `section` | string | `3.3` | Parent section (Afdeling) number |
| `hierarchy_path` | string | `Wet IB 2001 > Hfdst. 3 > Afd. 3.3 > Art. 3.114 > Lid 1` | Full lineage for citation |
| `effective_date` | date | `2024-01-01` | Temporal filter — current law |
| `expiry_date` | date\|null | `null` | Temporal filter — excludes repealed law |
| `version` | int | `3` | Monotonic — for historical queries |
| `security_classification` | enum | `PUBLIC` | DLS routing |
| `source_url` | string | `https://wetten.overheid.nl/...` | Traceability |
| `parent_chunk_id` | string\|null | `WetIB2001-2024::art3.114::root` | Parent-child link |
| `language` | string | `nl` | For multilingual queries |
| `ecli_id` | string\|null | `ECLI:NL:HR:2023:1234` | Case law identifier |
| `amendment_refs` | list | `["WetIB2001-2024-amend1"]` | Links to amending documents |
| `chunk_sequence` | int | `1` | Order within parent node |
| `token_count` | int | `287` | For context window management |
| `ingestion_timestamp` | datetime | `2026-04-11T22:00:00Z` | Audit trail |

`hierarchy_path` is included verbatim in every chunk sent to the LLM. The generation prompt requires `[Source: chunk_id | hierarchy_path]` on every factual claim. This is how the LLM knows a chunk belongs to "Article 3.114, Paragraph 2" — it is in the serialized text, not inferred.

## 1.4 Pseudo-code excerpt — structural parsing

Full file: [module1_ingestion.py](../pseudocode/module1_ingestion.py). Core of the `_split_node()` method:

```python
for artikel_match in self.ARTIKEL_RE.finditer(doc.text):
    artikel_num = artikel_match.group(2)
    parent_meta = {
        **doc_meta,
        "article_num": artikel_num,
        "hierarchy_path": f"{doc_meta['title']} > Art. {artikel_num}",
        "parent_chunk_id": None,
    }
    for lid_idx, lid_text in enumerate(self._split_on_lid(artikel_body), start=1):
        child_id = f"{doc_meta['doc_id']}::art{artikel_num}::lid{lid_idx}::chunk001"
        child_meta = {
            **parent_meta,
            "chunk_id": child_id,
            "paragraph_num": str(lid_idx),
            "hierarchy_path": parent_meta["hierarchy_path"] + f" > Lid {lid_idx}",
            "parent_chunk_id": f"{doc_meta['doc_id']}::art{artikel_num}::root",
        }
        node = TextNode(text=lid_text, metadata=child_meta, id_=child_id)
        node.relationships[NodeRelationship.PARENT] = RelatedNodeInfo(node_id=parent_meta["parent_chunk_id"])
        nodes.append(node)
```

Metadata flows parent → child and is never regenerated from text. A `lid` chunk inherits `article_num`, `doc_id`, `effective_date`, and `security_classification` from its article, which inherits from the document.

## 1.5 Vector DB selection — OpenSearch 2.15+

**OpenSearch wins on four properties no alternative matches simultaneously:**
1. Native Document-Level Security applied inside the engine, before scoring — the foundation of Module 4's mathematical proof.
2. Unified hybrid search: BM25 and kNN share one index, one DLS policy, one query. No cross-system consistency problems.
3. Self-hostable on government cloud (A1, A2).
4. Production-proven in EU government deployments.

Acknowledged tradeoff: pure-vector recall is marginally lower than Qdrant or Milvus on isolated benchmarks. For this workload — where DLS is non-negotiable — the unified model is worth it. Full alternative comparison: [Appendix B](#appendix-b--rejected-alternatives).

## 1.6 HNSW parameters + memory math

```json
"method": {
  "name": "hnsw",
  "engine": "nmslib",
  "space_type": "cosinesimil",
  "parameters": { "m": 16, "ef_construction": 256 }
}
```

| Parameter | Value | Justification |
|---|---|---|
| `m` | 16 | Standard balance: m=8 loses ~5% recall, m=32 doubles memory for ~2% gain. |
| `ef_construction` | 256 | One-time build cost. High = better graph quality. OpenSearch's recommended default. |
| `ef_search` | 128 | Query-time recall/latency tradeoff. p99 < 100 ms at 20M vectors per OpenSearch benchmarks. |

**Memory math:**

```
Raw vectors (fp32):   20M × 1024 dim × 4 B  = 81.92 GB
fp16 vectors:         20M × 1024 dim × 2 B  = 40.96 GB
  + HNSW graph:       40.96 GB × 1.5×       ≈ 61 GB total     ← PRIMARY
SQ8 vectors:          20M × 1024 dim × 1 B  = 20.48 GB
  + HNSW graph:       20.48 GB × 1.5×       ≈ 31 GB total     ← FALLBACK
```

fp16 is the primary recommendation: negligible recall loss (<0.5%), no tuning needed. SQ8 (~31 GB) is available for memory-constrained deployments at ~1–2% recall loss; compensate by raising `ef_search` from 128 to 192. Sharded across 6 shards, ~10 GB per shard on the fp16 path — fits a 3-node cluster with 32 GB RAM per node.

Cold segments (historical legislation, expired articles) use OpenSearch's on-disk mode, trading ~50 ms latency for ~10× memory savings on rarely-accessed data.

## 1.7 Temporal versioning — the expired-law trap

Every chunk carries `effective_date` and `expiry_date`. Default query filter:

```json
{
  "bool": {
    "filter": [
      { "range": { "effective_date": { "lte": "now" } } },
      { "bool": { "should": [
          { "bool": { "must_not": { "exists": { "field": "expiry_date" } } } },
          { "range": { "expiry_date": { "gt": "now" } } }
      ]}}
    ]
  }
}
```

Historical queries override with an explicit `reference_date`. This prevents citing the 2022 Box 1 rate for a 2024 question, and enables "what was the law on $DATE" queries for audit purposes.

## 1.8 Ingestion pipeline (offline)

LlamaIndex `IngestionPipeline`: Document Loader (pdfplumber / lxml / XML) → LegalDocumentChunker → Temporal Versioning Stamp → Embedding (multilingual-e5-large, `"passage: "` prefix, batch 64) → OpenSearch Bulk Indexing (upsert by chunk_id) → Cache Invalidation Callback.

Full implementation: [module1_ingestion.py](../pseudocode/module1_ingestion.py).

---

# Module 2 — Retrieval Strategy

> Pseudo-code: [module2_retrieval.py](../pseudocode/module2_retrieval.py) · Diagram: [retrieval_flow.md](../diagrams/retrieval_flow.md)

## 2.1 The dual nature of legal retrieval

Legal queries are bimodal: users either cite exact identifiers (`ECLI:NL:HR:2023:1234`, "Artikel 3.114") or describe concepts informally ("can I deduct home office expenses?"). A pure-vector system loses on the first — `ECLI:NL:HR:2023:1234` and `ECLI:NL:HR:2023:1235` have cosine ≈ 0.99 under any general embedding model. A pure-BM25 system loses on the second — "home office" shares zero tokens with the Dutch legal term `werkruimte in de eigen woning`. The right architecture uses both, and adds an exact-ID fast path for explicit references.

## 2.2 Three retrieval paths

```
query_type?
   │              │              │
REFERENCE       SIMPLE        COMPLEX
   │              │              │
exact_id_       hybrid_       For each sub-query:
retrieve()      retrieve()    hybrid_retrieve()
                (BM25 ∥       merge + dedupe
                 kNN + RRF)
   └──────────────┼──────────────┘
                  ▼
           rerank_chunks(top_k=8)
                  ▼
           top-8 → grade_context()
```

- **Exact-ID**: triggered when `classify_query()` detects `ECLI_PATTERN` or `ARTIKEL_PATTERN` regex match. Skips embedding and reranking on unambiguous single-match. Latency ~15 ms. When only an article number is detected without a law name, falls back to hybrid search with a `article_num` field boost. For law-name disambiguation, resolve to canonical `doc_id` via alias lookup before retrieval.
- **BM25**: `multi_match` on `chunk_text`, `title`, `hierarchy_path` with boosts. `dutch_legal_analyzer` (stemmer, stop words, ASCII-folding). Top-20.
- **kNN**: HNSW k-NN query with ef_search=128, temporal pre-filter (not post-filter). HyDE-transformed query for SIMPLE/COMPLEX types. Top-20.

BM25 and kNN run in **parallel** (ThreadPoolExecutor max_workers=2). Wall time = max(~20 ms, ~80 ms) = ~80 ms.

## 2.3 Fusion — RRF, not alpha blending

```
RRF_score(d) = Σ  1 / (k + rank_i(d))
              i ∈ {BM25, kNN}

where  k = 60  (Cormack, Clarke & Büttcher 2009)
```

| Property | RRF | Alpha blending |
|---|---|---|
| Score normalization needed | No (rank-based) | Yes (BM25 and cosine are on different scales) |
| Robust to distribution shifts | Yes | No |
| Hyperparameter sensitivity | Low (single k) | High (α must be retuned per query class) |
| Legal domain fit | BM25 scores are spiky for exact legal terms; cosine compresses in [0.65, 0.85] — RRF handles both naturally | Poor without per-query re-normalization |

Why k=60: Cormack et al. showed k∈[40,100] is robust across TREC tracks. 60 is the de-facto default used by Microsoft Bing, Elasticsearch, and OpenSearch's native RRF pipeline. Configured in [opensearch_index_mapping.json `_search_pipeline_config`](../schemas/opensearch_index_mapping.json) as the primary path; Python RRF (`_rrf_fuse()`) serves as fallback. Both produce identical top-40 lists.

## 2.4 Reranking — cross-encoder cascade

Model: **`BAAI/bge-reranker-v2-m3`**, self-hosted.

- **Multilingual**: Dutch, French, German, English — critical for A5.
- **Self-hosted**: model weights in the government cloud, satisfies A2.
- **Cross-encoder**: joint attention on (query, chunk) pairs yields higher precision than bi-encoders.
- **Rejected Cohere Rerank v3**: hosted API only, violates A2.

40 `(query, chunk_text)` pairs in one GPU call → top-8. Latency ~200 ms.

## 2.5 Top-K cascade

| Stage | Top-K | Why |
|---|---|---|
| BM25 | 20 | Balanced with kNN for RRF |
| kNN | 20 | Covers semantic neighborhood at ef_search=128 |
| RRF output | 40 | Reranker latency is linear: 40 → ~200 ms; 100 → ~500 ms (blows budget) |
| Reranker output | 8 | 8 × ~512 tokens ≈ 4 KB — fits any 8K+ LLM window with headroom for the system prompt |

Constants at [module3_crag_statemachine.py:68-69](../pseudocode/module3_crag_statemachine.py#L68-L69).

## 2.6 DLS integration

The DLS filter is applied **by OpenSearch**, **before** BM25 and kNN scoring. `_knn_retrieve()` passes the tier filter as a k-NN *pre-filter* parameter in the OpenSearch DSL — not a post-filter. Post-filtering on dense retrieval leaks via result count variance. The full argument is in §4.7.

---

# Module 3 — Agentic RAG & Self-Healing

> Pseudo-code: [module3_crag_statemachine.py](../pseudocode/module3_crag_statemachine.py), [module3_grader.py](../pseudocode/module3_grader.py) · Diagram: [crag_state_machine.md](../diagrams/crag_state_machine.md)

## 3.1 Why linear RAG fails

A common but insufficient pattern:

```python
chain = retriever | reranker | llm
answer = chain.invoke(query)
```

This has zero gates. If retrieval returns noise, the LLM generates a confident wrong answer — violating A14. This design inserts two gates: a grading gate between retrieval and generation, and a citation-validation gate between generation and response. Either can route to REFUSE.

## 3.2 Query classification

`classify_query()` at [line 162](../pseudocode/module3_crag_statemachine.py#L162):

| Type | Detector | Transformation |
|---|---|---|
| **REFERENCE** | Regex: ECLI or `Artikel N.M` | Pass-through; `exact_id_retrieve` |
| **SIMPLE** | ≤1 clause or LLM classifier | Apply HyDE if no legal terminology |
| **COMPLEX** | Multi-clause or LLM classifier | Decompose into ≤3 sub-queries |

## 3.3 Query transformation — HyDE and decomposition

**HyDE**: for SIMPLE queries without Dutch legal terminology. Generates a hypothetical Dutch legal passage and uses its embedding for retrieval — bridges the vocabulary gap between casual language and formal Dutch legal text. Latency ~300–500 ms; worth it only when needed. Prompt: [prompts/hyde_prompt.txt](../prompts/hyde_prompt.txt).

**Not applied when**: REFERENCE (already precise), retry attempts (avoid double-HyDE), COMPLEX (use decomposition instead).

**Decomposition**: for COMPLEX queries. Example: *"I'm a freelancer with a home office — what can I deduct, and do I need to charge BTW?"* → (1) werkruimte aftrek, (2) BTW-plicht ondernemer, (3) zelfstandigenaftrek. Max 3 sub-queries (3 × parallel retrieval = 3 × 80 ms). Results merged and deduped by `chunk_id`. Prompt: [prompts/decomposition_prompt.txt](../prompts/decomposition_prompt.txt).

## 3.4 The 9-state machine

```
                ┌──────────────────────────┐
                │      RECEIVE_QUERY       │
                └────────────┬─────────────┘
                             │
                             ▼
                ┌──────────────────────────┐
                │     TRANSFORM_QUERY      │
                │     HyDE / decompose     │
                └────────────┬─────────────┘
                             │
 ┌──────────────▶ ┌──────────────────────────┐
 │               │        RETRIEVE          │
 │               │  exact-id / hybrid / RRF │
 │               │  then rerank top-8       │
 │               └────────────┬─────────────┘
 │                            │
 │               ┌──────────────────────────┐
 │               │     GRADE_CONTEXT        │
 │               │  RetrievalGrader (LLM)   │
 │               └────────────┬─────────────┘
 │               route_after_grading(state)
 │          AMBIGUOUS       RELEVANT     IRRELEVANT
 │         and retry<1         │             │
 │               │             │             │
 │               ▼             │             │
 │  ┌──────────────────────┐   │             │
 │  │  REWRITE_AND_RETRY   │   │             │
 │  │   retry_count += 1   │   │             │
 │  └──────────┬───────────┘   │             │
 └─────────────┘               │             │
                               ▼             │
                  ┌──────────────────────────┐│
                  │       GENERATE           ││
                  │  LLM @ T=0.0 + citations ││
                  └────────────┬─────────────┘│
                               ▼              │
                  ┌──────────────────────────┐ │
                  │    VALIDATE_OUTPUT       │ │
                  │  citation set-membership │ │
                  └────────────┬─────────────┘ │
            route_after_validation(state)       │
            VALID          INVALID              │
              ▼               │                 │
        ┌──────────┐          │                 │
        │  RESPOND │          ▼                 ▼
        └────┬─────┘   ┌─────────────────────────┐
             │         │         REFUSE          │
             │         └────────────┬────────────┘
             └──────────────────────┘
                        ▼
                      [END]
```

State table and LangGraph wiring: [module3_crag_statemachine.py:884](../pseudocode/module3_crag_statemachine.py#L884). Full diagram: [crag_state_machine.md](../diagrams/crag_state_machine.md).

## 3.5 Retrieval Evaluator (Grader)

Batched LLM call scoring all 8 chunks in one prompt (~150 ms). Three labels: RELEVANT / AMBIGUOUS / IRRELEVANT + confidence + reasoning. Full implementation: [module3_grader.py](../pseudocode/module3_grader.py). Prompt: [prompts/grader_prompt.txt](../prompts/grader_prompt.txt).

**Query-type-aware aggregation thresholds** (mature CRAG avoids over-refusing on reference queries):

| Query type | RELEVANT threshold | Rationale |
|---|---|---|
| REFERENCE (ECLI / Article exact) | ≥ 1 chunk with confidence ≥ 0.8 | One authoritative source is sufficient for a specific article lookup |
| SIMPLE (single concept) | ≥ 2 chunks with confidence ≥ 0.6 | Corroboration needed; one chunk may lack context |
| COMPLEX (multi-part) | ≥ 3 chunks with confidence ≥ 0.6 | Multiple provisions typically needed |

- `AMBIGUOUS` = majority AMBIGUOUS, OR below threshold but >0 RELEVANT
- `IRRELEVANT` = 0 RELEVANT and majority IRRELEVANT

**Temporal awareness**: chunks whose `expiry_date < now` are downgraded from RELEVANT to AMBIGUOUS even if content matches. Prevents the "repealed article as current law" failure.

## 3.6 Fallback decision table

Direct answer to the assessment question. Routing function: `route_after_grading()` at [line 840](../pseudocode/module3_crag_statemachine.py#L840).

| GradingResult | retry_count | Next state | Rationale |
|---|---|---|---|
| **RELEVANT** | any | `GENERATE` | Threshold met. Proceed to LLM answer generation at T=0.0 with forced citations. |
| **AMBIGUOUS** | `< 1` | `REWRITE_AND_RETRY` | Partial signal. LLM rewrites query with more specific Dutch legal terminology. HyDE disabled on retry (avoid double-HyDE). Loop back to RETRIEVE. |
| **AMBIGUOUS** | `≥ 1` | `REFUSE` | Budget exhausted. Polite bilingual refusal with structured log. |
| **IRRELEVANT** | any | `REFUSE` | Out of scope. No retry would help. Refuse immediately. |

```python
def route_after_grading(state: CRAGState) -> Literal["generate", "rewrite_and_retry", "refuse"]:
    if state["grading_result"] == GradingResult.RELEVANT:
        return "generate"
    if state["grading_result"] == GradingResult.AMBIGUOUS and state["retry_count"] < MAX_RETRIES:
        return "rewrite_and_retry"
    return "refuse"   # IRRELEVANT, or AMBIGUOUS with retries exhausted
```

## 3.7 Generation with mandatory citations

Temperature `0.0`. System prompt forces `[Source: chunk_id | hierarchy_path]` on every factual claim. Excerpt:

```
You MUST:
1. Use ONLY information from the provided context. Do not use prior knowledge.
2. Cite EVERY factual claim with [Source: chunk_id | hierarchy_path].
3. If citations cannot be produced for a claim, omit the claim entirely.
```

Full prompt: [prompts/generator_system_prompt.txt](../prompts/generator_system_prompt.txt).

## 3.8 Post-generation citation validation

`validate_output()` at [line 587](../pseudocode/module3_crag_statemachine.py#L587). Two conditions must hold:

1. At least one citation is present.
2. Every cited `chunk_id` exists in the graded context set (catches fabricated citations that pass the format check but reference non-existent chunks).

If either fails, routes to REFUSE. The system is **fail-closed**: the validation mechanism minimizes hallucination risk by refusing when citation verification fails. It does not claim mathematical certainty — groundedness is enforced by the combination of grader-gated generation (G2), T=0.0 generation (G3), and set-membership validation (G4).

## 3.9 Five anti-hallucination gates

| # | Gate | Location | What it prevents |
|---|---|---|---|
| **G1** | RBAC pre-filter | OpenSearch DLS (before scoring) | Retrieving documents above the user's tier |
| **G2** | Retrieval grader | `grade_context()` → `route_after_grading()` | Generating from irrelevant context |
| **G3** | Citation format constraint | Generator system prompt at T=0.0 | LLM inventing unstructured citations |
| **G4** | Citation set-membership | `validate_output()` → `route_after_validation()` | LLM fabricating chunk_ids that match format but don't exist |
| **G5** | Bounded retry | MAX_RETRIES = 1 | Infinite rewrite loops; SLO blow-outs |

## 3.10 MAX_RETRIES = 1 — the TTFT math

```
Happy path TTFT        :  ~1450 ms  ✓  (within 1500 ms)
One retry adds         :   ~580 ms  (rewrite 150 + retrieval 80 + rerank 200 + grader 150)
Worst case, 1 retry    :  ~2030 ms  ✗  (over hard limit)
Expected TTFT, 1 retry :  ~1450 + 0.15 × 580 ≈ ~1537 ms  ← ~acceptable; rare exceedances
Worst case, 2 retries  :  ~2610 ms  ✗  (expected TTFT also exceeds 1500 ms)
```

`MAX_RETRIES = 1` is the largest value that keeps expected TTFT plausibly under budget. Two retries make even the expected case unacceptable. On budget exhaustion, the system refuses — A16 (false negatives > false positives).

## 3.11 Two worked traces

**Trace 1 — Happy path** ("Wat is de arbeidskorting voor 2024?")

```
RECEIVE_QUERY   → SIMPLE, should_use_hyde=True
TRANSFORM_QUERY → HyDE: "Op grond van artikel 3.114 Wet IB 2001..."
RETRIEVE        → top-8 (Art. 3.114 lid 1 at rank #1 via RRF)
GRADE_CONTEXT   → 6 RELEVANT, 2 AMBIGUOUS → RELEVANT
GENERATE        → "De arbeidskorting bedraagt 5.532 euro [Source: WetIB2001-2024::art3.114::lid1::chunk001 | ...]"
VALIDATE_OUTPUT → 2 cited ids, both in graded context → valid
RESPOND → END   (Latency: ~1250 ms ✓)
```

**Trace 2 — Irrelevant → refusal** ("Who built the Eiffel Tower?")

```
RETRIEVE        → 40 tax law chunks, none about Eiffel Tower
GRADE_CONTEXT   → 0 RELEVANT, 7 IRRELEVANT → IRRELEVANT
REFUSE → END    (Latency: ~600 ms; no generation, no retry)
Response: "I could not find relevant Dutch tax-law information. This system is scoped to Dutch tax authority documents."
```

---

# Module 4 — Production Ops, Security & Evaluation

> Pseudo-code: [module4_cache.py](../pseudocode/module4_cache.py) · Schema: [rbac_roles.json](../schemas/rbac_roles.json) · Metrics: [metrics_matrix.md](../eval/metrics_matrix.md) · CI: [eval_gate.yml](../.github/workflows/eval_gate.yml)

## 4.1 Semantic cache — design

The cache is a **wrapper around** the CRAG state machine, not a node inside it. A cache hit returns the stored response without entering the graph — this is where "~15 ms TTFT for repeat queries" originates.

```python
def handle_query(query, user_security_tier, session_id):
    cached = semantic_cache.check_cache(query, user_security_tier)
    if cached:
        return cached.response_text, cached.citations   # ~15 ms
    result = invoke_crag(query, user_security_tier, session_id)
    if result["final_response"]:
        semantic_cache.store_cache(...)
    return result["final_response"], result["final_citations"]
```

Backend: Redis Stack + RediSearch HNSW, cosine similarity, tier-partitioned TAG field. Entry: `{query_text, query_embedding, response_text, citations, retrieved_doc_ids, security_tier, ttl_seconds, query_type}`. Full implementation: [module4_cache.py](../pseudocode/module4_cache.py).

## 4.2 The 0.97 threshold — specific justification

**The safe threshold for financial/tax data is cosine ≥ 0.97.**

```
Query A: "Box 1 tarief 2024"
Query B: "Box 1 tarief 2023"

cosine(A, B) ≈ 0.94 under multilingual-e5-large
```

The two queries have different answers. A 0.90 threshold serves last year's rate for this year's question — a fiscal error. At 0.97 the year-confusion case is excluded (0.94 < 0.97 → cache miss → full pipeline). Genuine paraphrase hits still land (e.g., "Wat is het Box 1 tarief 2024?" vs "Box 1 tarief voor 2024?" ≈ 0.985). This is a direct application of A14 and A16.

## 4.3 TTL strategy

| Query type | TTL | Reason |
|---|---|---|
| Case law (ECLI) | **0 s (no cache)** | New rulings can overturn interpretations |
| Procedural | **7 days** | Most stable content type |
| Default | **24 hours** | Amendments happen; 24h caps stale-answer exposure |

## 4.4 Cache invalidation on re-index

On document re-index, the ingestion pipeline calls `semantic_cache.invalidate_by_doc_ids([re_indexed_doc_id])`. Scans all cache entries where `retrieved_doc_ids` intersects the re-indexed set and purges them. Prevents stale answers after legal amendments — the coupling between Module 1 and Module 4.

## 4.5 Cache tier partitioning

Redis key format: `cache:{security_tier}:{hash(query_embedding)}`.

Lookup uses a **RediSearch TAG pre-filter** to exclude inaccessible tiers *before* KNN scoring:

```python
accessible_tiers = get_accessible_tiers(user_security_tier)  # e.g. ["PUBLIC", "INTERNAL"]
tier_filter = "|".join(accessible_tiers)
query = f"(@security_tier:{{{tier_filter}}})=>[KNN 1 @embedding $vec AS score]"
results = redis.ft("tax_rag_cache").search(query, {"vec": vec})
if results and results[0].score >= 0.97:
    return results[0]
```

Tier hierarchy: `PUBLIC < INTERNAL < RESTRICTED < CLASSIFIED_FIOD`. A helpdesk user (INTERNAL tier) cannot hit a RESTRICTED or FIOD cache entry regardless of similarity score.

## 4.6 RBAC — 4 tiers, 6 roles

| OpenSearch role | PUBLIC | INTERNAL | RESTRICTED | CLASSIFIED_FIOD |
|---|:---:|:---:|:---:|:---:|
| `role_public_user` | ✓ | | | |
| `role_helpdesk` | ✓ | ✓ | | |
| `role_tax_inspector` | ✓ | ✓ | ✓ | |
| `role_legal_counsel` | ✓ | ✓ | ✓ | |
| `role_fiod_investigator` | ✓ | ✓ | ✓ | ✓ |
| `role_ingestion_service` | write-only (no search) | | | |

Identity flow: AD group → IdP (OIDC) → JWT → API Gateway → OpenSearch impersonation header → DLS role resolution. Full DLS JSON: [schemas/rbac_roles.json](../schemas/rbac_roles.json).

**DLS filter for `role_helpdesk`:**

```json
{
  "bool": {
    "must_not": [
      { "term": { "security_classification": "CLASSIFIED_FIOD" } },
      { "term": { "security_classification": "RESTRICTED" } }
    ]
  }
}
```

Applied by the OpenSearch Security Plugin before BM25/kNN scoring. Application code never receives or enumerates forbidden documents.

## 4.7 Pre-retrieval vs post-retrieval — the mathematical proof

**Filtering must happen pre-retrieval, inside the search engine, before scoring occurs.**

Post-retrieval filtering leaks information about classified documents via three channels even when the filtered output contains no classified content.

**Leakage Mode 1 — Result count variance**

Let `|S_c|/|S| = 0.05` (5% of corpus is classified) and `k = 40`.

```
Under post-filter:  returned_count = k − c
P(c ≥ 1) = 1 − (1 − |S_c|/|S|)^k = 1 − 0.95^40 ≈ 0.87
```

On 87% of queries the user observes fewer than k results and can infer "classified documents relevant to my query exist." This is an information leak about `S_c` even though no classified content was shown.

**Leakage Mode 2 — Ranking distortion**

BM25/kNN scoring under post-filter operates on all of `S`. The relative ranking of permitted documents is influenced by classified competitors. Different classified sets → different rankings for the same permitted content — detectable by a user with multiple access levels.

**Leakage Mode 3 — Timing side-channel**

Post-filter adds processing time proportional to the filtered count `c`. Statistical timing analysis over repeated queries can infer `c`.

**Under pre-retrieval filtering:**

```
S_user = S \ S_forbidden
```

All three modes are eliminated: result count is `min(k, |relevant ∩ S_user|)` — independent of `|S_c|`. Scoring competes only permitted documents. Timing depends on `|S_user|`, not `|S_c|`. ∎

Proof also in [rbac_roles.json](../schemas/rbac_roles.json) and [diagrams/security_model.md §5](../diagrams/security_model.md).

## 4.8 Three attack scenarios (thwarted)

| Attack | Mechanism | Defense |
|---|---|---|
| Direct classified query from helpdesk | "transfer pricing fraud methods" | DLS pre-filter: FIOD docs excluded before scoring → 0 relevant chunks → REFUSE |
| Cache poisoning via similar query | Helpdesk query ≈ FIOD investigator's cached query | TAG pre-filter excludes FIOD cache entry → cache MISS → safe retrieval |
| Timing side-channel | Repeated queries to measure response variance | Pre-filter: response time independent of `\|S_c\|` → no signal |

## 4.9 CI/CD evaluation pipeline — 4 stages

Workflow stub: [.github/workflows/eval_gate.yml](../.github/workflows/eval_gate.yml).

| Stage | Trigger | Gate | Blocks |
|---|---|---|---|
| **1. PR** | Pull request | Context Precision@8 ≥ 0.85, NDCG@8 ≥ 0.75 | Merge |
| **2. Staging** | Merge to main | Faithfulness ≥ 0.90, Citation Accuracy = 1.0, Hallucination Rate ≤ 0.02 | Deploy |
| **3. Canary** | 5% traffic / 2h | TTFT p95 > 1500 ms, refusal rate > 20%, error rate > 1% | Auto-rollback |
| **4. Production** | Full rollout | Continuous monitoring + weekly 5% LLM-as-judge | Alert / rollback |

**Embedding model deploys** require Stage 1 re-baseline. **LLM deploys** require Stage 2 re-baseline. Both require Stage 3 canary.

## 4.10 Exact metrics — Ragas & DeepEval

### Retrieval Quality — Stage 1 gate

| Metric | Tool | Threshold |
|---|---|---|
| **Context Precision@8** | **Ragas** | **≥ 0.85** |
| Context Recall | Ragas | ≥ 0.80 |
| NDCG@8 | pytrec_eval | ≥ 0.75 |
| MRR | custom | ≥ 0.85 |
| Exact-ID Recall | custom | = 1.00 |

### Generation Quality — Stage 2 gate

| Metric | Tool | Threshold |
|---|---|---|
| **Faithfulness** | **Ragas / DeepEval** | **≥ 0.90** |
| Answer Relevance | Ragas | ≥ 0.85 |
| Citation Accuracy | custom (binary) | = 1.00 |
| Hallucination Rate | DeepEval | ≤ 0.02 |

### Security — Continuous / absolute

| Metric | Tool | Threshold |
|---|---|---|
| TTFT p95 | OpenTelemetry / Prometheus | < 1500 ms |
| **DLS Bypass Rate** | OpenSearch Audit Log | **= 0.00** |
| **Cache Cross-Tier Contamination** | custom | **= 0.00** |
| Audit Log Completeness | OpenTelemetry | = 100% |

DLS Bypass Rate and Cache Cross-Tier Contamination are absolute-zero: any positive value triggers immediate incident response. Full matrix: [eval/metrics_matrix.md](../eval/metrics_matrix.md). Sample test set: [eval/golden_test_set_spec.json](../eval/golden_test_set_spec.json).

## 4.11 Golden test set

- **200+ query-document pairs**, versioned alongside the evaluation pipeline (sample in [eval/golden_test_set_spec.json](../eval/golden_test_set_spec.json)).
- **Distribution**: 40% simple factual, 30% complex multi-part, 20% reference (ECLI / Artikel), 10% adversarial (cross-tier attempts, temporal traps, citation-fabrication triggers).
- **Language mix**: 80% Dutch, 15% English, 5% mixed.
- Maintained by legal domain experts + ML team; updated quarterly or on significant legislation changes.

## 4.12 Observability

| Concern | Tool | Purpose |
|---|---|---|
| Distributed tracing | OpenTelemetry → Jaeger | Per-query span across every state machine node |
| Metrics | Prometheus + Grafana | TTFT p50/p95/p99, cache hit rate, refusal rate |
| Structured logs | JSON → OpenSearch audit index | Query, retrieval, generation, access decisions |
| LLM observability | LangSmith / Arize Phoenix | Prompt/response logs, token usage, cost |
| Alerting | Prometheus Alertmanager | TTFT p95 > 1500 ms → page on-call; DLS Bypass > 0 → CRITICAL |

Satisfies A18. Every query, retrieval, generation, and access decision is captured and queryable for auditors.

---

# Appendix A — Repository Structure

```
assesmentemre/
├── assesment.txt                              (original brief, untouched)
├── requirements.txt                           (Python dependencies)
├── .github/
│   └── workflows/eval_gate.yml               (CI/CD 4-stage gate stub)
├── drafts/
│   ├── final_submission_v2.md                 (THIS FILE — the assessment answer)
│   ├── module1_draft.md                       (extended Module 1 narrative)
│   ├── module2_draft.md                       (extended Module 2 narrative)
│   ├── module3_draft.md                       (extended Module 3 narrative)
│   └── module4_draft.md                       (extended Module 4 narrative)
├── pseudocode/
│   ├── module1_ingestion.py                   (LegalDocumentChunker + pipeline)
│   ├── module2_retrieval.py                   (3-path retrieval + RRF + rerank)
│   ├── module3_crag_statemachine.py           (LangGraph StateGraph, 9 states)
│   ├── module3_grader.py                      (RetrievalGrader with batch mode)
│   └── module4_cache.py                       (Redis Stack semantic cache)
├── schemas/
│   ├── chunk_metadata.json                    (22-field metadata schema)
│   ├── opensearch_index_mapping.json          (HNSW m=16, fp16, RRF pipeline, DLS)
│   └── rbac_roles.json                        (4 tiers, 6 roles, mathematical proof)
├── diagrams/
│   ├── architecture_overview.md               (anchor diagram)
│   ├── retrieval_flow.md                      (Module 2 visual + worked example)
│   ├── crag_state_machine.md                  (Module 3 visual + trace examples)
│   └── security_model.md                      (Module 4 visual + DLS proof)
├── prompts/
│   ├── grader_prompt.txt                      (RELEVANT/AMBIGUOUS/IRRELEVANT + few-shot)
│   ├── generator_system_prompt.txt            (forced citation format)
│   ├── hyde_prompt.txt                        (Dutch legal hypothetical generation)
│   └── decomposition_prompt.txt               (max 3 sub-queries)
├── eval/
│   ├── metrics_matrix.md                      (Ragas + DeepEval gate thresholds)
│   └── golden_test_set_spec.json              (5-entry schema sample; full set: 200+)
├── performance/
│   └── resource_allocation.md                 (supplementary: GPU budgets, QPS, cost/query)
├── reference/
│   ├── assumptions.md                         (A1–A18 with architectural impact)
│   └── tools_and_technologies.txt             (full stack inventory with versions)
└── internal/
    ├── master_feedback.md                     (working notes — not part of submission)
    └── final_submission_v1.md                 (original v1, superseded by v2)
```

---

# Appendix B — Rejected Alternatives

### B.1 Naive recursive text splitting
`RecursiveCharacterTextSplitter(chunk_size=512)` cuts at character counts. A chunk containing "the rate is 37%" no longer contains "art. 3.114, lid 2". Citation reconstruction becomes impossible. (§1.1)

### B.2 Pure vector search (no BM25)
`ECLI:NL:HR:2023:1234` and `ECLI:NL:HR:2023:1235` have cosine ≈ 0.99 — the wrong ruling is indistinguishable from the right one. BM25 handles exact tokens precisely.

### B.3 Post-retrieval RBAC filtering
Three leakage modes: result count variance (`P ≈ 0.87`), ranking distortion, timing side-channel. Pre-retrieval DLS eliminates all three. (§4.7)

### B.4 LLM-only citation generation
LLMs invent plausible-looking article numbers and ECLI references without structural constraints. We force `[Source: chunk_id | hierarchy_path]` format and set-membership-validate every citation against retrieved context.

### B.5 Linear chain without retrieval grading
`retriever | reranker | llm` has no gate — the LLM generates confidently from noise. The CRAG state machine inserts a grading gate as the architectural answer.

### B.6 Pinecone / Weaviate Cloud
SaaS. Dutch tax authority data including FIOD material cannot leave national jurisdiction (A2). Self-hosted OpenSearch is the only option that unifies DLS + hybrid search + sovereignty.

### B.7 Cohere Rerank v3
Probably the strongest general-domain reranker, but hosted API only. Data sovereignty (A2) forbids sending query text to Cohere. `BAAI/bge-reranker-v2-m3` is self-hosted and multilingual.

### B.8 Plain LangChain (without LangGraph)
LangChain's `|` operator supports sequential pipelines but not conditional edges, loops, or an explicit REFUSE state. LangGraph's `StateGraph` supports all three as first-class constructs.

### B.9 Aggressive semantic cache (threshold < 0.95)
"Box 1 tarief 2024" and "Box 1 tarief 2023" have cosine ≈ 0.94. A 0.90 threshold returns the wrong year's rate. (§4.2)

### B.10 External LLM APIs without data governance
Using OpenAI or Anthropic APIs directly sends government tax data to US providers, violating A2. Acceptable alternatives: self-hosted Mixtral/Llama, or Azure OpenAI Government Cloud with explicit data residency guarantees.

---

# Appendix C — Tools & Technologies

| Category | Component | Version | Purpose |
|---|---|---|---|
| Search | OpenSearch + k-NN plugin | 2.15+ | Hybrid search + DLS |
| Cache | Redis Stack + RediSearch | 7.4+ | Semantic cache |
| Ingestion | LlamaIndex | 0.11+ | NodeParser, IngestionPipeline |
| Embedding | multilingual-e5-large | — | 1024-dim embeddings |
| Reranker | BAAI/bge-reranker-v2-m3 | — | Cross-encoder reranker |
| Orchestration | LangGraph | 0.2+ | CRAG StateGraph |
| LLM (self-host) | Mixtral 8x22B via vLLM | 0.5+ | Generation |
| LLM (cloud alt.) | Azure OpenAI Gov Cloud | — | GPT-4 with data residency |
| API | FastAPI + Uvicorn | 0.111+ | Async HTTP layer |
| Evaluation | Ragas + DeepEval | 0.2+ / 1.0+ | Context Precision, Faithfulness |
| Observability | OpenTelemetry + Jaeger | 1.25+ | Distributed tracing |
| Observability | Prometheus + Grafana | — | Metrics + dashboards |
| Security | OpenSearch Security Plugin | bundled | RBAC + DLS |

Full inventory: [tools_and_technologies.txt](../reference/tools_and_technologies.txt).

---

# Appendix D — Glossary

| Term | Definition |
|---|---|
| **BM25** | Sparse retrieval scoring based on term frequency. Dominant keyword algorithm in OpenSearch. |
| **CRAG** | Corrective RAG. Grading gate between retrieval and generation; refuses when context is insufficient. |
| **DLS** | Document-Level Security. OpenSearch capability applying role-bound filter queries before scoring. |
| **ECLI** | European Case Law Identifier. Format: `ECLI:NL:{court}:{year}:{number}`. |
| **FIOD** | Fiscale Inlichtingen- en Opsporingsdienst. Dutch tax fraud investigation branch. |
| **HNSW** | Hierarchical Navigable Small World. Graph-based Approximate Nearest Neighbor index. |
| **HyDE** | Hypothetical Document Embeddings. LLM generates a hypothetical answer; its embedding is used for retrieval. |
| **RRF** | Reciprocal Rank Fusion. `RRF(d) = Σ 1/(k + rank_i(d))`, standard k=60. |
| **SQ8** | Scalar Quantization 8-bit. fp32 → int8, ~4× memory reduction, ~1–2% recall loss. |
| **TTFT** | Time To First Token. Wall-clock from query submission to first LLM token. |

---

**END OF SUBMISSION**

*This is the revised submission (v2). The original v1 is preserved at [internal/final_submission_v1.md](../internal/final_submission_v1.md). All pseudo-code, schemas, prompts, and evaluation artifacts are under the repository root. Full module narratives are in [drafts/](../drafts/).*
