# Module 1 — Ingestion & Knowledge Structuring

> **Assessment questions answered in this module**
> 1. How do you ensure the LLM knows a chunk belongs to "Article 3.114, Paragraph 2"?
> 2. Brief pseudo-code / config showing how metadata is preserved.
> 3. Which Vector DB do you select for 500,000 documents / 20M+ chunks?
> 4. Exact index configurations (HNSW m, ef_construction).
> 5. Memory optimization / quantization to prevent OOM errors.

---

## 1.1 The hidden failure of recursive text splitters

The most common mistake in legal RAG is to reach for `RecursiveCharacterTextSplitter(chunk_size=512, chunk_overlap=50)` and call it done. That class does not know what a legal document is. It sees a string of characters and cuts it at the nearest whitespace to its target length.

Concretely, a recursive splitter applied to Wet IB 2001 produces chunks like:

```
  ... de arbeidskorting bedraagt ingevolge artikel
  [CHUNK BOUNDARY]
  3.114, eerste lid, voor het kalenderjaar 2024...
```

The chunk containing "bedraagt 5.532 euro" no longer contains "art. 3.114, eerste lid". The LLM receives a numerically-correct statement about the employment tax credit but has no evidence of which article of which law it comes from. Ask that LLM "cite your source" and it will do one of two things:

1. Refuse (best case — but the user now receives no answer for a question the system *should* have answered).
2. Hallucinate a plausible-looking citation (worst case — a fabricated legal reference in a Dutch tax authority response). This is exactly the failure mode Assumption [A14](../notes/assumptions.md) (zero-hallucination tolerance) and [A12](../notes/assumptions.md) (exact citations required) forbid.

Structure-aware chunking prevents both failures by treating the legal hierarchy as the primary splitting signal and character count as a secondary constraint.

---

## 1.2 Chunking strategy — structure-aware parsing

Dutch legal documents have an explicit hierarchy (Assumption [A8](../notes/assumptions.md)):

```
  Wet (Act)
   └─ Hoofdstuk (Chapter)
       └─ Afdeling (Section)
           └─ Artikel (Article)
               └─ Lid (Paragraph)
                   └─ Sub (Sub-paragraph: a, b, i, ii, ...)
```

Our chunker walks this tree and emits one chunk per leaf node. The boundary rules differ by document type:

| Document type | Primary boundary | Secondary boundary | Why |
|---|---|---|---|
| **LEGISLATION** (Wet IB, AWR, Wet OB) | Artikel / Lid | Sub-paragraph | Articles are the unit of legal reference; a user citation "art. 3.114 lid 2" must map to exactly one chunk |
| **CASE_LAW** (ECLI rulings) | Overweging (r.o. X.Y) | — | Each consideration is an independent legal argument and cited independently |
| **POLICY** (Handboek, internal manuals) | Hoofdstuk / Paragraaf | Numbered section headings | Procedural docs have author-defined hierarchy; use it |
| **ELEARNING** (training modules) | Module / Lesson | — | Training content is topic-based, not legally precedent-setting |

**Chunk size targets.** Our chunks target **256–512 tokens** but the boundary is authoritative. If an Article is 180 tokens, the chunk is 180 tokens. If a single Paragraph is 800 tokens, we split it internally (sentence-boundary split with 64-token overlap) but every sub-chunk inherits the parent Paragraph's metadata. **Structural splits have zero overlap** because the boundary itself is semantically meaningful.

**Why zero overlap at structural boundaries.** With naive splitters, overlap is there to paper over arbitrary cuts. With structural splits, the cut lines up with a legal boundary — overlap would mean duplicating content under two different citations, which corrupts deduplication and inflates the index. Overlap is only used *inside* oversized structural units where a secondary split was needed.

---

## 1.3 Metadata schema — answering the citation question

The answer to the assessment's first question ("How do you ensure the LLM knows a chunk belongs to Article 3.114, Paragraph 2?") is: **every chunk carries a metadata dictionary that encodes its full legal lineage, and this dictionary is both indexed for filtering and included in the context the LLM sees.**

The schema is defined formally in [schemas/chunk_metadata.json](../schemas/chunk_metadata.json). Summary of the 22 fields:

| Field | Type | Purpose | Example |
|---|---|---|---|
| `chunk_id` | keyword | Deterministic ID, used for upsert and citation validation | `WetIB2001-2024::art3.114::par1::chunk001` |
| `doc_id` | keyword | Document ID with version suffix | `WetIB2001-2024` |
| `doc_type` | enum | LEGISLATION / CASE_LAW / POLICY / ELEARNING | `LEGISLATION` |
| `title` | text | Full official title | `Wet inkomstenbelasting 2001` |
| `article_num` | keyword | Article number within the document | `3.114` |
| `paragraph_num` | keyword | Paragraph (lid) number | `1` |
| `sub_paragraph` | keyword | Sub-paragraph letter or roman | `a` |
| `chapter` | keyword | Chapter identifier | `3` |
| `section` | keyword | Section within chapter | `3.1` |
| `hierarchy_path` | text | Full breadcrumb from root | `Wet IB 2001 > Hoofdstuk 3 > Art 3.114 > Lid 1` |
| `effective_date` | date | When this version became legally effective | `2024-01-01` |
| `expiry_date` | date\|null | When this version was superseded | `null` (currently active) |
| `version` | integer | Monotonic version number | `12` |
| `security_classification` | enum | RBAC pivot field | `PUBLIC` |
| `source_url` | uri | Link back to authoritative source | `https://wetten.overheid.nl/BWBR0011353` |
| `parent_chunk_id` | keyword | Parent in the hierarchy tree | `WetIB2001-2024::art3.114` |
| `language` | enum | `nl`, `en`, or `nl-en` | `nl` |
| `ecli_id` | keyword | For CASE_LAW only, enables exact-ID shortcut | `ECLI:NL:HR:2023:1234` |
| `amendment_refs` | array | Cross-references to amended/amending docs | `[WetIB2001-2023]` |
| `chunk_sequence` | integer | Order within parent structural unit | `0` |
| `token_count` | integer | Tokens per chunk (for budgeting) | `412` |
| `ingestion_timestamp` | datetime | When indexed (drives cache invalidation) | `2024-06-15T14:30:00Z` |

**The three fields that directly answer the assessment question**: `hierarchy_path`, `article_num`, and `paragraph_num`. The LLM sees them in the retrieved context and uses them to compose the citation token the generation prompt requires: `[Source: {chunk_id} | {hierarchy_path}]`. See [prompts/generator_system_prompt.txt](../prompts/generator_system_prompt.txt) for the exact prompt instruction that forces this.

**Deterministic chunk IDs.** The `chunk_id` follows the format `{doc_id}::{structural_parts}::chunk{seq:03d}`. Identical input produces an identical chunk_id on re-ingestion, which means OpenSearch can **upsert** (update-or-insert) rather than create duplicates. This is critical for Assumption [A7](../notes/assumptions.md) (nightly batch re-index) — without deterministic IDs, the index would double in size on every re-run.

---

## 1.4 Pseudo-code — the metadata inheritance mechanism

The key code path is the `MetadataInheritanceManager.create_chunk_metadata()` method in [pseudocode/module1_ingestion.py](../pseudocode/module1_ingestion.py) (around line 424). A child chunk inherits from its ancestors via a two-dict merge:

```python
# From pseudocode/module1_ingestion.py — abbreviated for draft readability
def create_chunk_metadata(
    doc_meta: DocumentLevelMetadata,
    boundary: StructuralBoundary,
    parent_hierarchy: dict,
    chunk_sequence: int,
    token_count: int,
) -> ChunkMetadata:
    # 1. Start with everything the parent knows (chapter, section, article, ...)
    current_hierarchy = dict(parent_hierarchy)

    # 2. Layer on this boundary's own level
    if boundary.level == "chapter":
        current_hierarchy["chapter"] = boundary.identifier
    elif boundary.level == "section":
        current_hierarchy["section"] = boundary.identifier
    elif boundary.level == "article":
        current_hierarchy["article_num"] = boundary.identifier
    elif boundary.level == "paragraph":
        current_hierarchy["paragraph_num"] = boundary.identifier
    elif boundary.level == "sub_paragraph":
        current_hierarchy["sub_paragraph"] = boundary.identifier

    # 3. Deterministic chunk_id built from the accumulated hierarchy
    chunk_id = build_chunk_id(
        doc_id=doc_meta.doc_id,
        article_num=current_hierarchy.get("article_num"),
        paragraph_num=current_hierarchy.get("paragraph_num"),
        sub_paragraph=current_hierarchy.get("sub_paragraph"),
        chapter=current_hierarchy.get("chapter"),
        section=current_hierarchy.get("section"),
        chunk_sequence=chunk_sequence,
    )

    # 4. Human-readable breadcrumb for the LLM to cite
    hierarchy_path = build_hierarchy_path(
        doc_meta=doc_meta,
        chapter=current_hierarchy.get("chapter"),
        section=current_hierarchy.get("section"),
        article_num=current_hierarchy.get("article_num"),
        paragraph_num=current_hierarchy.get("paragraph_num"),
        sub_paragraph=current_hierarchy.get("sub_paragraph"),
    )
    # → e.g. "Wet IB 2001 > Hoofdstuk 3 > Art 3.114 > Lid 1"

    # 5. Every inherited field from the document-level metadata flows through
    return ChunkMetadata(
        chunk_id=chunk_id,
        doc_id=doc_meta.doc_id,
        doc_type=doc_meta.doc_type,
        title=doc_meta.title,
        effective_date=doc_meta.effective_date,
        expiry_date=doc_meta.expiry_date,
        version=doc_meta.version,
        security_classification=doc_meta.security_classification,
        source_url=doc_meta.source_url,
        language=doc_meta.language,
        # ... structural fields from current_hierarchy
        chapter=current_hierarchy.get("chapter"),
        article_num=current_hierarchy.get("article_num"),
        paragraph_num=current_hierarchy.get("paragraph_num"),
        sub_paragraph=current_hierarchy.get("sub_paragraph"),
        hierarchy_path=hierarchy_path,
        chunk_sequence=chunk_sequence,
        token_count=token_count,
    )
```

The full `LegalDocumentChunker._parse_nodes()` loop also sets `NodeRelationship.PARENT` and `NodeRelationship.CHILD` links on every LlamaIndex `TextNode`, so retrieval can optionally walk up from a Lid chunk to its parent Artikel chunk for additional context (Hierarchical Retrieval). See [pseudocode/module1_ingestion.py:607-755](../pseudocode/module1_ingestion.py).

**What an emitted chunk looks like** (pruned for brevity):

```json
{
  "chunk_id": "WetIB2001-2024::art3.114::par1::chunk001",
  "chunk_text": "De arbeidskorting bedraagt voor de belastingplichtige ...",
  "doc_id": "WetIB2001-2024",
  "doc_type": "LEGISLATION",
  "title": "Wet inkomstenbelasting 2001",
  "article_num": "3.114",
  "paragraph_num": "1",
  "hierarchy_path": "Wet IB 2001 > Hoofdstuk 3 > Art 3.114 > Lid 1",
  "effective_date": "2024-01-01",
  "expiry_date": null,
  "version": 12,
  "security_classification": "PUBLIC",
  "parent_chunk_id": "WetIB2001-2024::art3.114::chunk000",
  "language": "nl",
  "token_count": 287,
  "embedding": [0.0193, -0.0412, ..., 0.0087]
}
```

---

## 1.5 Vector database selection — why OpenSearch 2.15+

The corpus is 500K documents × ~40 chunks each = ~20M chunks (Assumption [A6](../notes/assumptions.md)). At that scale, and under the sovereignty and RBAC constraints of Assumptions [A1](../notes/assumptions.md), [A2](../notes/assumptions.md), and [A17](../notes/assumptions.md), the vector DB choice is narrow. We select **OpenSearch 2.15+ with the k-NN plugin**.

**Decision matrix — rejected alternatives:**

| Candidate | Why rejected |
|---|---|
| **Pinecone** | SaaS, US-hosted. Tax authority data (incl. FIOD fraud investigations) cannot leave national jurisdiction. A2 blocks this outright. Also: no Document-Level Security — RBAC would have to be an application-layer filter, which §1.7 of this draft and [Module 4](module4_draft.md) prove is unsafe. |
| **Weaviate Cloud** | Same SaaS / sovereignty problem as Pinecone. Self-hosted Weaviate exists but its multi-tenancy / DLS story is less mature than OpenSearch. |
| **Qdrant** | Excellent pure-vector performance, self-hostable. But: (a) no native BM25 — hybrid search requires a sidecar Elasticsearch/OpenSearch, i.e. two datastores to keep in sync; (b) no native document-level security — would need application-layer filtering, same flaw as Pinecone. The split architecture doubles operational burden and introduces a security gap between the two stores. |
| **Milvus** | Strong at scale, but operational complexity is high (separate Pulsar/Kafka, etcd, MinIO components) and DLS requires external enforcement. A team supporting a production tax system does not want to operate Milvus's full dependency stack. |
| **pgvector** | Works for hundreds of thousands of vectors; breaks down at tens of millions. No HNSW tuning knobs comparable to OpenSearch's k-NN plugin, no native BM25 fusion. Wrong tool for 20M chunks. |
| **Elasticsearch** | Technically capable (it is OpenSearch's ancestor), but its SSPL license is problematic for some government procurement contexts. OpenSearch is Apache 2.0 and has a direct fork heritage. |

**Why OpenSearch wins:**

1. **Unified hybrid search in one engine.** BM25 (via Lucene) and dense k-NN (via the k-NN plugin with nmslib/faiss engines) share the same index, the same query DSL, the same filter pipeline. Retrieval is one call, not two.
2. **Native Document-Level Security** via the OpenSearch Security Plugin. The DLS filter is applied *inside the search engine, before BM25 scoring and kNN distance computation*. This is the only way to prevent the information leakage modes that §1.7 outlines (and that [diagrams/security_model.md §5](../diagrams/security_model.md) proves formally).
3. **Self-hostable.** Runs on-premises or in Azure Gov / AWS GovCloud. Satisfies [A1](../notes/assumptions.md) and [A2](../notes/assumptions.md).
4. **Government battle-tested.** Already deployed in multiple EU public sector systems, well understood by security audit teams. Lower procurement friction than a newer engine.

**Acknowledged tradeoff.** Pure-vector benchmark numbers for Qdrant and Milvus can be ~15–25% lower in p99 latency than OpenSearch at the same recall target. We accept this gap because it is measured in the tens of milliseconds (well within the 80 ms retrieval budget in [§6 of the architecture overview](../diagrams/architecture_overview.md)) and because the gains do not compensate for losing unified hybrid search and native DLS.

---

## 1.6 HNSW parameters — justified with math

Full config in [schemas/opensearch_index_mapping.json](../schemas/opensearch_index_mapping.json):

```json
"embedding": {
  "type": "knn_vector",
  "dimension": 1024,
  "method": {
    "name": "hnsw",
    "space_type": "cosinesimil",
    "engine": "nmslib",
    "parameters": {
      "m": 16,
      "ef_construction": 256
    }
  }
}
```

With `"knn.algo_param.ef_search": 128` set at the index-settings level.

**Why `m = 16`.** HNSW's `m` is the number of bidirectional graph links each node maintains. Recall increases with `m` but memory does too:

| m | Graph memory overhead (20M nodes, 8B per link) | Recall@10 on MS MARCO (published benchmarks) |
|---|---:|---:|
| 8 | ~2.5 GB | 0.89 |
| **16** | **~5.1 GB** | **0.94** |
| 32 | ~10.2 GB | 0.96 |
| 64 | ~20.5 GB | 0.97 |

m=16 is the knee of the curve: 94% recall at 5 GB. m=32 buys 2 points of recall at 2× memory; we route the saved RAM budget into a larger `ef_search` instead, which is tunable at query time.

**Why `ef_construction = 256`.** This controls the depth of the nearest-neighbor search during index build. 256 is a high-quality build (one-time cost, run offline in the ingestion pipeline). Lower values (64, 128) sacrifice graph quality to save indexing time, which would be penny-wise for a system where indexing is nightly and queries happen thousands of times per day.

**Why `ef_search = 128`.** This is the runtime breadth of the search walk. 128 gives ~95% recall@10 on our benchmark with p99 latency in the 60–80 ms range at 20M chunks. It is tunable per-query, so low-latency cache-miss paths can drop to 96 while high-recall analyst workflows can raise to 256. We set the default at 128.

**Engine: nmslib.** OpenSearch exposes three engines — `nmslib`, `faiss`, and `lucene`. nmslib is the incumbent and has the fastest HNSW for cosine similarity on normalized vectors. faiss is preferred if you want Product Quantization; we use Scalar Quantization instead (§1.7), which nmslib handles natively.

---

## 1.7 Memory optimization — preventing the 80 GB OOM

**The raw memory arithmetic.** 20M vectors × 1024 dimensions × 4 bytes (fp32) = **81.9 GB just for the raw embeddings**. Plus HNSW graph overhead at m=16 (~5 GB) plus the inverted index for BM25 fields plus doc store plus OS file cache. A 64 GB-RAM node cannot hold the working set. A naive deployment OOMs on the first large query burst.

**Our mitigation: Scalar Quantization to int8 (SQ8).** OpenSearch's k-NN plugin supports `encoder: sq` on the knn_vector field, which stores each of the 1024 dimensions as one byte instead of four. The arithmetic becomes:

```
  Raw vectors (SQ8):  20M × 1024 × 1 B  ≈  20.5 GB
  HNSW graph (m=16):                     ≈   5.1 GB
  BM25 inverted index (rough):           ≈  15 GB
  Doc store + metadata:                  ≈  10 GB
  Total working set (1 replica):         ≈  50-55 GB
```

Spread across a 3-node cluster with 64 GB RAM each (headroom for OS cache, JVM heap, request burst) this is comfortable. A 2-node cluster is possible with 96 GB nodes if cost is a constraint.

**Recall impact of SQ8.** Published OpenSearch k-NN benchmarks show <2% recall loss on MS MARCO when moving from fp32 to SQ8. We verify this on the golden test set during pre-deployment evaluation (see [Module 4](module4_draft.md) §4) and gate on it: if Context Precision@8 drops below 0.85 after enabling SQ8, we fall back to fp16 (40 GB raw — doubles memory but zero measurable recall loss).

**Why not Product Quantization (PQ).** PQ would reduce raw vectors to ~5 GB (a further 4× reduction) but introduces two costs: (a) non-trivial tuning curve (number of sub-vectors, code book training), (b) larger latency variance because decoding is not free. For a 20M corpus we are not memory-desperate after SQ8, so the additional complexity is not justified. If the corpus grew to 200M chunks, PQ would become the right call.

**Other OOM levers** (applied in addition to SQ8):

| Lever | Effect | Cost |
|---|---|---|
| **On-disk mode for cold segments** | Historical / expired chunks (see §1.8) live on SSD, not RAM | +20 ms latency on cold queries — acceptable since the temporal filter means cold segments are rarely hit |
| **6 shards × 1 replica** | Parallel query execution; each shard is ~3.3M chunks → ~10 GB, matching OpenSearch's 10–50 GB/shard guidance | 1 replica = 2× storage; acceptable for HA |
| **Dedicated coordinator nodes** | Separates search coordination from data holding | Three extra small nodes; negligible in a gov-cloud budget |
| **`refresh_interval: 30s`** | Batch new segments every 30 s instead of 1 s default | Slight staleness in cache-miss paths during nightly re-index, but ingest throughput 10× higher |

These together give a stable memory profile under the concurrent-user load described in Assumption [A10](../notes/assumptions.md) (200–500 simultaneous queries).

---

## 1.8 Temporal versioning — the expired-law trap

Legislation is amended. A 2022 version of article 3.114 differs from the 2024 version by several hundred euros in the arbeidskorting amount. If the index contains both and the retriever returns either, the system has a non-trivial probability of citing a repealed rate as current law. This is an Assumption [A14](../notes/assumptions.md) violation even without any "hallucination" in the LLM's own output — the failure is upstream.

**Bi-temporal model.** Every chunk carries two dates:

- `effective_date` — when this version became legally effective.
- `expiry_date` — when it was superseded (null = currently active).

**Default query-time filter** (applied to every retrieval unless the caller explicitly opts out for a historical query):

```
  effective_date <= NOW  AND  (expiry_date IS NULL OR expiry_date > NOW)
```

This filter is built once and attached as an OpenSearch `filter` clause for both the BM25 and kNN legs of hybrid search, so it pre-filters candidates before scoring — identical mechanism to the DLS filter in §1.9.

**Historical queries.** A legal counsel user researching the 2022 version of an amended article passes a `reference_date` parameter that replaces `NOW` in the filter. The current-law default is safe; the historical mode is explicit and audit-logged.

**Upsert on re-index.** When Wet IB 2001 is amended, the ingestion pipeline:

1. Re-indexes the new version of the amended articles (new chunks with new `effective_date`).
2. Updates the `expiry_date` of the superseded chunks to the amendment date (they stay in the index, findable only by historical queries).
3. Calls `semantic_cache.invalidate_by_doc_ids([amended_doc_id])` — see [Module 4 §3](module4_draft.md).

Step 3 is critical: without cache invalidation, a cached answer referencing the expired chunks would survive and poison subsequent queries for up to 24 hours (the default cache TTL).

---

## 1.9 Ingestion pipeline (offline path)

The online query path and the offline ingestion path are fully separated. Ingestion runs as a nightly batch job (Assumption [A7](../notes/assumptions.md)) or on-demand when a document management system event signals a new publication. The pipeline is LlamaIndex's `IngestionPipeline` with these transformations:

```
  Source Documents (PDF / HTML / XML from wetten.overheid.nl,
    rechtspraak.nl, internal CMS, FIOD document stores)
                      │
                      ▼
  Document Loader (pdfplumber / lxml / unstructured)
    - Text extraction
    - Initial metadata capture from DMS / classification manifest
                      │
                      ▼
  LegalDocumentChunker (custom NodeParser, §1.2-1.4)
    - Structure-aware splitting
    - Metadata inheritance
    - Deterministic chunk_id generation
    - Parent-child NodeRelationship links
                      │
                      ▼
  Temporal Versioning Stamp
    - effective_date / expiry_date / version assignment
                      │
                      ▼
  Embedding Generation (HuggingFaceEmbedding)
    - Model: multilingual-e5-large
    - E5 prefix: "passage: " at indexing, "query: " at search
    - Batch size: 64 chunks per GPU call
    - Output: 1024-dim fp32 (quantized to int8 on write)
                      │
                      ▼
  OpenSearch Bulk Indexing
    - Index: tax_authority_rag_chunks
    - Upsert by chunk_id (deterministic → no duplicates)
    - Bulk size: 500 docs per request
                      │
                      ▼
  Cache Invalidation Callback
    - For each re-indexed doc_id:
        semantic_cache.invalidate_by_doc_ids([doc_id])
    - See module4_cache.py
```

**Why `multilingual-e5-large`** (Assumption [A5](../notes/assumptions.md)): the corpus is primarily Dutch with some English EU regulations and CJEU case law. An English-only model (e5-large-v2, bge-base-en) would fail on Dutch legal jargon. The multilingual variant is trained on the mC4 / MIRACL / NusaX suite and produces aligned embeddings across Dutch, English, and German, which matches our corpus language mix. Same model is used at indexing time and query time — they share a vector space by construction, so the "passage:" / "query:" prefixes are the only asymmetry.

**Ingestion is the only write path** into the OpenSearch index. The query path in [Module 2](module2_draft.md) and [Module 3](module3_draft.md) is pure-read. This separation is what makes cache invalidation reliable: the callback fires exactly when a document's chunks change, and at no other time.

---

## 1.10 Supporting artifacts

| Artifact | Purpose |
|---|---|
| [schemas/chunk_metadata.json](../schemas/chunk_metadata.json) | Formal JSON Schema for the 22-field chunk metadata model |
| [schemas/opensearch_index_mapping.json](../schemas/opensearch_index_mapping.json) | Complete OpenSearch index mapping (mappings, settings, analyzers, HNSW, DLS hooks) |
| [pseudocode/module1_ingestion.py](../pseudocode/module1_ingestion.py) | Full `LegalDocumentChunker`, `MetadataInheritanceManager`, `IngestionPipeline` |
| [diagrams/architecture_overview.md §4](../diagrams/architecture_overview.md) | Ingestion pipeline diagram in system context |
| [notes/assumptions.md](../notes/assumptions.md) | A5 (Dutch corpus), A6 (scale), A7 (batch), A8 (hierarchy), A12 (citations), A14 (zero-hallucination) |
| [tools_and_technologies.txt](../tools_and_technologies.txt) | LlamaIndex, OpenSearch, multilingual-e5-large versions |

---

**Ends Module 1.** Module 2 takes the indexed chunks and shows how we retrieve them.
