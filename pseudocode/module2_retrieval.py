"""
Module 2: Retrieval Strategy — Hybrid Search + RRF Fusion + Cross-Encoder Reranking
====================================================================================

This module answers the assessment questions:
  "Design an advanced retrieval strategy using hybrid search."
  "How do you balance precision vs. recall in a legal retrieval context?"
  "Explain your ranking/reranking approach and justify your top-k choices."

Design principles:
  1. Three retrieval paths — exact-ID shortcut, BM25 sparse, kNN dense.
  2. RRF fusion — rank-based, no score normalization needed, robust across distributions.
  3. Cross-encoder reranking — precision filter after wide initial retrieval.
  4. Pre-retrieval security — DLS enforced by OpenSearch, NOT application-level filtering.
  5. Temporal filtering — only return currently-effective legal provisions by default.
  6. Parallel execution — BM25 and kNN run concurrently to minimize latency.

Stack: OpenSearch 2.15+ (k-NN plugin), multilingual-e5-large, bge-reranker-v2-m3.

Latency budget (retrieval + reranking):
  Embedding:   ~30ms  (query → 1024-dim vector)
  BM25:        ~20ms  (inverted index lookup, 20M docs)
  kNN:         ~80ms  (HNSW with ef_search=128, 20M vectors)
  Parallel:    ~80ms  (BM25 and kNN run concurrently → max(20, 80))
  RRF fusion:   ~5ms  (simple rank merge in Python)
  Reranking:  ~200ms  (cross-encoder over 40 chunks, batched)
  ─────────────────
  Total:      ~315ms  (well within the ~450ms retrieval budget)

This module exports three functions consumed by the CRAG state machine
(module3_crag_statemachine.py, line 383):
  - hybrid_retrieve(query, user_security_tier, top_k) → list[dict]
  - exact_id_retrieve(reference, user_security_tier, top_k) → list[dict]
  - rerank_chunks(query, chunks, top_k) → list[dict]
"""

import re
import hashlib
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
from opensearchpy import OpenSearch
from transformers import AutoTokenizer, AutoModel, AutoModelForSequenceClassification
import torch


# =============================================================================
# 1. CONSTANTS — Retrieval configuration
# =============================================================================

OPENSEARCH_INDEX = "tax_authority_rag_chunks"
"""Index name matching opensearch_index_mapping.json."""

BM25_TOP_K = 20
"""
Top-k for BM25 sparse retrieval. 20 results from the inverted index.
Combined with kNN top-20, gives 40 candidates for RRF fusion.
"""

KNN_TOP_K = 20
"""
Top-k for kNN dense retrieval. 20 results from HNSW vector search.
ef_search=128 (set in index settings) provides good recall/latency tradeoff.
"""

RRF_RANK_CONSTANT = 60
"""
RRF formula: score(d) = Σ 1/(k + rank_i(d))
k=60 is the standard constant from the original RRF paper (Cormack et al., 2009).
Higher k reduces the influence of top-ranked documents; 60 is the industry default.
Also configured in opensearch_index_mapping.json _rrf_alternative.
"""

RERANKER_MODEL_NAME = "BAAI/bge-reranker-v2-m3"
"""
Multilingual cross-encoder reranker. Selected because:
  - Multilingual support (Dutch + English legal texts)
  - Self-hosted (no data leaves the network — Assumption A2)
  - Strong reranking quality on MTEB benchmarks
  - Acceptable latency: ~5ms per (query, passage) pair → 40 pairs ≈ 200ms batched
Alternative: cross-encoder/ms-marco-MiniLM-L-12-v2 (English-only, faster, lower quality).
"""

EMBEDDING_MODEL_NAME = "intfloat/multilingual-e5-large"
"""
1024-dimensional multilingual embedding model. Same model used for indexing
in module1_ingestion.py. Query prefix: "query: " (E5 instruction format).
Passage prefix during indexing: "passage: " (applied in module1_ingestion.py).
"""

EMBEDDING_DIM = 1024


# =============================================================================
# 2. OPENSEARCH CLIENT — Connection with DLS-aware authentication
# =============================================================================

class OpenSearchClient:
    """
    Thin wrapper around the OpenSearch Python client.

    CRITICAL SECURITY DESIGN:
      The client passes the user's security tier to OpenSearch via request headers.
      OpenSearch resolves this to a DLS role (from rbac_roles.json) and applies
      the document-level security filter BEFORE any scoring occurs.

      We do NOT filter documents in Python. That would be post-retrieval filtering,
      which leaks information (see rbac_roles.json mathematical_proof_pre_retrieval).

    In production, the user's JWT token from the API gateway is forwarded to
    OpenSearch, which maps it to the correct DLS role via the Security Plugin.
    Here we simulate this with an impersonation header.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 9200,
        use_ssl: bool = True,
        http_auth: tuple = ("admin", "admin"),
    ):
        self.client = OpenSearch(
            hosts=[{"host": host, "port": port}],
            http_auth=http_auth,
            use_ssl=use_ssl,
            verify_certs=True,
            ssl_show_warn=False,
        )

    def search(
        self,
        body: dict,
        user_security_tier: str,
        index: str = OPENSEARCH_INDEX,
    ) -> dict:
        """
        Execute a search with DLS enforcement.

        The user_security_tier is passed as an impersonation header.
        OpenSearch's Security Plugin resolves this to the matching DLS role:
          - "PUBLIC"          → role_public_user (sees only PUBLIC docs)
          - "INTERNAL"        → role_helpdesk (sees PUBLIC + INTERNAL)
          - "RESTRICTED"      → role_tax_inspector (sees PUBLIC + INTERNAL + RESTRICTED)
          - "CLASSIFIED_FIOD" → role_fiod_investigator (sees everything)

        The DLS filter query from rbac_roles.json is applied transparently
        by OpenSearch BEFORE any BM25 scoring or kNN distance calculation.
        """
        # Map security tier to OpenSearch role for DLS enforcement
        tier_to_role = {
            "PUBLIC": "role_public_user",
            "INTERNAL": "role_helpdesk",
            "RESTRICTED": "role_tax_inspector",
            "CLASSIFIED_FIOD": "role_fiod_investigator",
        }
        role = tier_to_role.get(user_security_tier, "role_public_user")

        headers = {
            "opendistro_security_impersonate_as": role,
        }

        return self.client.search(
            index=index,
            body=body,
            params={"search_pipeline": "tax_rag_hybrid_pipeline"},
            headers=headers,
        )


# Singleton client instance (connection pooled)
_os_client = OpenSearchClient()


# =============================================================================
# 3. EMBEDDING HELPER — Query embedding with E5 instruction prefix
# =============================================================================

# Model loaded once, reused across requests (thread-safe for inference)
_embedding_tokenizer = AutoTokenizer.from_pretrained(EMBEDDING_MODEL_NAME)
_embedding_model = AutoModel.from_pretrained(EMBEDDING_MODEL_NAME)
_embedding_model.eval()  # Inference mode


def embed_query(text: str) -> list[float]:
    """
    Embed a query string into a 1024-dimensional vector.

    Uses the E5 instruction format: prefix "query: " for queries.
    (At index time, module1_ingestion.py uses prefix "passage: " for documents.)

    This asymmetric prefix is critical for E5 model performance —
    without it, retrieval quality drops ~5-10% on benchmarks.

    Returns a normalized unit vector (L2 norm = 1.0) for cosine similarity.
    """
    # E5 requires "query: " prefix for query-side embeddings
    prefixed = f"query: {text}"

    inputs = _embedding_tokenizer(
        prefixed,
        max_length=512,
        padding=True,
        truncation=True,
        return_tensors="pt",
    )

    with torch.no_grad():
        outputs = _embedding_model(**inputs)

    # Mean pooling over token embeddings (E5 convention)
    attention_mask = inputs["attention_mask"]
    token_embeddings = outputs.last_hidden_state
    input_mask_expanded = attention_mask.unsqueeze(-1).expand(token_embeddings.size()).float()
    embedding = torch.sum(token_embeddings * input_mask_expanded, 1) / torch.clamp(
        input_mask_expanded.sum(1), min=1e-9
    )

    # L2 normalize for cosine similarity
    embedding = torch.nn.functional.normalize(embedding, p=2, dim=1)

    return embedding[0].tolist()


# =============================================================================
# 4. TEMPORAL FILTER — Only return currently-effective legal provisions
# =============================================================================

def build_temporal_filter(reference_date: Optional[str] = None) -> dict:
    """
    Build the temporal validity filter for legal documents.

    Default behavior (reference_date=None):
      Return only documents where:
        effective_date <= NOW  AND  (expiry_date IS NULL OR expiry_date > NOW)
      This ensures repealed or superseded articles are excluded.

    Historical mode (reference_date="2022-01-15"):
      Return documents effective on a specific date.
      Used when the user explicitly asks about historical law.

    Matches the _temporal_filter_template in opensearch_index_mapping.json.

    Why this matters:
      "Artikel 2.10 Wet IB 2001 (geldig tot 31-12-2022): tarief 37,07%"
      Without this filter, this expired provision would be retrieved for
      "What is the Box 1 rate for 2024?" — giving a dangerously wrong answer.
    """
    date_anchor = reference_date if reference_date else "now/d"

    return {
        "bool": {
            "must": [
                {
                    "range": {
                        "effective_date": {"lte": date_anchor}
                    }
                }
            ],
            "should": [
                {
                    "bool": {
                        "must_not": [
                            {"exists": {"field": "expiry_date"}}
                        ]
                    }
                },
                {
                    "range": {
                        "expiry_date": {"gt": date_anchor}
                    }
                }
            ],
            "minimum_should_match": 1,
        }
    }


# =============================================================================
# 5. EXACT-ID RETRIEVAL — Shortcut for ECLI and Article references
# =============================================================================

# Patterns matching those in module3_crag_statemachine.py (lines 86-98)
ECLI_PATTERN = re.compile(r"ECLI:NL:[A-Z]{2,}:\d{4}:[A-Z0-9]+")
ARTICLE_PATTERN = re.compile(
    r"[Aa]rt(?:ikel)?\s*(\d+[\.\:]?\d*[a-z]?)"
    r"(?:\s+(?:lid|par(?:agraaf)?)\s*(\d+))?"
)


def exact_id_retrieve(
    reference: str,
    user_security_tier: str,
    top_k: int = 8,
) -> list[dict]:
    """
    Bypass vector search for queries containing exact legal identifiers.

    Called by the CRAG state machine (module3_crag_statemachine.py, line 390)
    when query_type == REFERENCE.

    Two reference types:
      1. ECLI pattern (e.g., "ECLI:NL:HR:2023:1234")
         → keyword filter on `ecli_id` field
      2. Article pattern (e.g., "Artikel 3.114 lid 2")
         → keyword filter on `article_num` (+ optional `paragraph_num`)

    Why this path exists:
      Vector search is BAD at exact identifiers. "ECLI:NL:HR:2023:1234" and
      "ECLI:NL:HR:2023:1235" would have nearly identical embeddings (~0.99 cosine)
      but refer to completely different court rulings. Keyword matching is exact.

    DLS is enforced by OpenSearch — we just pass user_security_tier through.
    Temporal filter is applied to return only currently-effective versions.
    """
    temporal_filter = build_temporal_filter()

    # Detect reference type and build the appropriate keyword query
    ecli_match = ECLI_PATTERN.search(reference)
    article_match = ARTICLE_PATTERN.search(reference)

    if ecli_match:
        ecli_id = ecli_match.group(0)
        query_body = {
            "size": top_k,
            "query": {
                "bool": {
                    "must": [
                        {"term": {"ecli_id": ecli_id}}
                    ],
                    "filter": [temporal_filter],
                }
            },
            "_source": True,
        }

    elif article_match:
        article_num = article_match.group(1)
        paragraph_num = article_match.group(2)  # May be None

        must_clauses = [
            {"term": {"article_num": article_num}}
        ]
        if paragraph_num:
            must_clauses.append({"term": {"paragraph_num": paragraph_num}})

        query_body = {
            "size": top_k,
            "query": {
                "bool": {
                    "must": must_clauses,
                    "filter": [temporal_filter],
                }
            },
            "_source": True,
        }

    else:
        # Fallback: treat the entire reference as a keyword search on chunk_text
        query_body = {
            "size": top_k,
            "query": {
                "bool": {
                    "must": [
                        {"match_phrase": {"chunk_text": reference}}
                    ],
                    "filter": [temporal_filter],
                }
            },
            "_source": True,
        }

    # Execute with DLS enforcement
    response = _os_client.search(
        body=query_body,
        user_security_tier=user_security_tier,
    )

    return _parse_search_results(response)


# =============================================================================
# 6. BM25 SPARSE RETRIEVAL — Keyword-based search with Dutch legal analyzer
# =============================================================================

def _bm25_retrieve(
    query: str,
    user_security_tier: str,
    top_k: int = BM25_TOP_K,
) -> list[dict]:
    """
    BM25 sparse retrieval against chunk_text and title fields.

    Uses the dutch_legal_analyzer (defined in opensearch_index_mapping.json):
      - Dutch stemming (e.g., "belastingen" → "belasting")
      - Dutch stop words removed
      - ASCII folding (e.g., "coöperatie" → "cooperatie")

    Field boosting:
      - chunk_text^1.0  — primary content field (default weight)
      - title^0.5       — document title (half weight, avoids title-only matches)
      - hierarchy_path^0.3 — breadcrumb path (low weight, helps with section references)

    Why BM25 is essential alongside kNN:
      Legal queries often contain specific terminology that BM25 handles well:
        "aftrekbaarheid hypotheekrente" → exact term match in inverted index
        "artikel 3.120 Wet IB 2001" → keyword match (kNN would fuzz this)
      BM25 scores are spiky (exact match = very high score), which is desirable
      for legal precision.

    Temporal filter ensures only currently-effective provisions are returned.
    DLS is enforced by OpenSearch transparently.
    """
    temporal_filter = build_temporal_filter()

    query_body = {
        "size": top_k,
        "query": {
            "bool": {
                "must": [
                    {
                        "multi_match": {
                            "query": query,
                            "fields": [
                                "chunk_text^1.0",
                                "title^0.5",
                                "hierarchy_path^0.3",
                            ],
                            "type": "best_fields",
                            "analyzer": "dutch_legal_analyzer",
                        }
                    }
                ],
                "filter": [temporal_filter],
            }
        },
        "_source": True,
    }

    response = _os_client.search(
        body=query_body,
        user_security_tier=user_security_tier,
    )

    # Attach BM25 rank for RRF fusion
    results = _parse_search_results(response)
    for rank, chunk in enumerate(results):
        chunk["_bm25_rank"] = rank + 1  # 1-indexed rank
        chunk["_bm25_score"] = chunk.pop("_score", 0.0)

    return results


# =============================================================================
# 7. kNN DENSE RETRIEVAL — Semantic search via HNSW vector index
# =============================================================================

def _knn_retrieve(
    query: str,
    user_security_tier: str,
    top_k: int = KNN_TOP_K,
) -> list[dict]:
    """
    kNN dense retrieval using HNSW vector search.

    Pipeline:
      1. Embed the query using multilingual-e5-large (with "query: " prefix)
      2. Execute kNN search on the `embedding` field (1024-dim, cosinesimil)
      3. HNSW parameters: m=16, ef_search=128 (from index settings)

    Why kNN is essential alongside BM25:
      Conceptual queries without exact legal terms need semantic understanding:
        "Can I deduct home office expenses?" → semantically similar to
        "Artikel 3.17 Wet IB 2001: werkruimte eigen woning aftrekbaar"
      BM25 would miss this because there's no keyword overlap.

    IMPORTANT: The temporal filter is applied as a knn pre-filter, meaning
    OpenSearch filters BEFORE computing vector distances. This is more efficient
    than post-filtering and ensures we always get top_k results from the valid
    document set.

    DLS is enforced by OpenSearch transparently.
    """
    query_embedding = embed_query(query)
    temporal_filter = build_temporal_filter()

    query_body = {
        "size": top_k,
        "query": {
            "knn": {
                "embedding": {
                    "vector": query_embedding,
                    "k": top_k,
                    "filter": temporal_filter,
                }
            }
        },
        "_source": True,
    }

    response = _os_client.search(
        body=query_body,
        user_security_tier=user_security_tier,
    )

    # Attach kNN rank for RRF fusion
    results = _parse_search_results(response)
    for rank, chunk in enumerate(results):
        chunk["_knn_rank"] = rank + 1  # 1-indexed rank
        chunk["_knn_score"] = chunk.pop("_score", 0.0)

    return results


# =============================================================================
# 8. RRF FUSION — Reciprocal Rank Fusion
# =============================================================================

def _rrf_fuse(
    bm25_results: list[dict],
    knn_results: list[dict],
    k: int = RRF_RANK_CONSTANT,
) -> list[dict]:
    """
    Reciprocal Rank Fusion (RRF) to combine BM25 and kNN result lists.

    Formula:
      RRF_score(d) = Σ 1 / (k + rank_i(d))

    where k=60 (constant) and rank_i(d) is the rank of document d in list i.
    If a document appears in both lists, its RRF score is the sum of both
    reciprocal ranks. If it appears in only one list, the other term is 0.

    Why RRF over linear interpolation (alpha-blending):
      - BM25 scores are on an unbounded scale (0 to ~50+), spiky for exact matches
      - Cosine similarity scores are compressed (typically 0.60 to 0.85)
      - Alpha-blending (alpha * knn_score + (1-alpha) * bm25_score) requires
        score normalization, which is fragile and query-dependent
      - RRF treats them as RANK lists, ignoring score magnitude entirely
      - More robust when score distributions differ (Cormack et al., 2009)

    NOTE: OpenSearch 2.15+ supports RRF natively via search_pipeline (see
    opensearch_index_mapping.json _rrf_alternative). This Python implementation
    serves as a fallback and for clarity in the pseudo-code.
    """
    # Build a mapping: chunk_id → {chunk_data, rrf_score}
    fused: dict[str, dict] = {}

    for chunk in bm25_results:
        cid = chunk["chunk_id"]
        rank = chunk.get("_bm25_rank", len(bm25_results))
        rrf_contribution = 1.0 / (k + rank)

        if cid not in fused:
            fused[cid] = {**chunk, "_rrf_score": 0.0}
        fused[cid]["_rrf_score"] += rrf_contribution

    for chunk in knn_results:
        cid = chunk["chunk_id"]
        rank = chunk.get("_knn_rank", len(knn_results))
        rrf_contribution = 1.0 / (k + rank)

        if cid not in fused:
            fused[cid] = {**chunk, "_rrf_score": 0.0}
        fused[cid]["_rrf_score"] += rrf_contribution

    # Sort by RRF score descending
    fused_list = sorted(fused.values(), key=lambda x: x["_rrf_score"], reverse=True)

    return fused_list


# =============================================================================
# 9. HYBRID RETRIEVE — Main entry point (called by CRAG state machine)
# =============================================================================

def hybrid_retrieve(
    query: str,
    user_security_tier: str,
    top_k: int = 40,
) -> list[dict]:
    """
    Execute hybrid retrieval: BM25 + kNN in parallel → RRF fusion → top-k.

    Called by the CRAG state machine (module3_crag_statemachine.py, lines 400, 419).

    Pipeline:
      1. Embed query (once — shared by kNN; ~30ms)
      2. BM25 sparse retrieval (~20ms)  }  run in PARALLEL
      3. kNN dense retrieval (~80ms)     }  → max(20, 80) = ~80ms
      4. RRF fusion (~5ms)
      5. Return top-k fused results

    Total: ~115ms (embedding + parallel retrieval + fusion)
    This is well within the retrieval latency budget.

    Parameters:
      query: The search query (may be HyDE-transformed or a sub-query)
      user_security_tier: One of PUBLIC, INTERNAL, RESTRICTED, CLASSIFIED_FIOD.
                          Passed to OpenSearch for DLS enforcement.
      top_k: Number of results to return after fusion (default 40, input to reranker)

    Returns:
      list[dict] where each dict contains all chunk metadata fields from
      opensearch_index_mapping.json (chunk_id, chunk_text, hierarchy_path,
      title, article_num, paragraph_num, effective_date, etc.)
    """
    # ── Run BM25 and kNN in parallel for minimum latency ──
    # ThreadPoolExecutor is used because both operations are I/O-bound
    # (network calls to OpenSearch). GIL is not a bottleneck here.
    with ThreadPoolExecutor(max_workers=2) as executor:
        bm25_future = executor.submit(
            _bm25_retrieve, query, user_security_tier, BM25_TOP_K
        )
        knn_future = executor.submit(
            _knn_retrieve, query, user_security_tier, KNN_TOP_K
        )

        bm25_results = bm25_future.result()
        knn_results = knn_future.result()

    # ── Fuse via RRF ──
    fused_results = _rrf_fuse(bm25_results, knn_results, k=RRF_RANK_CONSTANT)

    # Return top-k after fusion
    return fused_results[:top_k]


# =============================================================================
# 10. CROSS-ENCODER RERANKING — Precision filter
# =============================================================================

# Load reranker model once, reuse across requests
_reranker_tokenizer = AutoTokenizer.from_pretrained(RERANKER_MODEL_NAME)
_reranker_model = AutoModelForSequenceClassification.from_pretrained(RERANKER_MODEL_NAME)
_reranker_model.eval()


def rerank_chunks(
    query: str,
    chunks: list[dict],
    top_k: int = 8,
) -> list[dict]:
    """
    Cross-encoder reranking of retrieved chunks.

    Called by the CRAG state machine (module3_crag_statemachine.py, line 426).
    NOTE: Reranks against the ORIGINAL query, not the HyDE-transformed text.
    This is intentional — the reranker should assess relevance to what the
    user actually asked, not to the hypothetical answer.

    Pipeline:
      1. For each chunk, create a (query, chunk_text) pair
      2. Batch-encode all pairs through the cross-encoder
      3. Score each pair (higher = more relevant)
      4. Sort by score descending, return top-k

    Why cross-encoder over bi-encoder for reranking:
      - Cross-encoders process query+passage jointly (full attention)
      - Bi-encoders process them independently (no cross-attention)
      - Cross-encoders achieve ~5-10% higher NDCG but are slower
      - Acceptable for reranking (40 pairs) but too slow for initial retrieval (20M docs)

    Latency: ~200ms for 40 chunks (batched GPU inference).
      40 pairs × ~5ms/pair = 200ms. Fits within the reranking budget.
      For 8 chunks (exact-ID path): ~40ms.

    Parameters:
      query: The original user query (NOT HyDE-transformed)
      chunks: List of chunk dicts from hybrid_retrieve or exact_id_retrieve
      top_k: Number of top-scoring chunks to return (default 8)

    Returns:
      list[dict] — top-k chunks sorted by cross-encoder relevance score,
      with _rerank_score added to each chunk dict.
    """
    if not chunks:
        return []

    # Build (query, passage) pairs for the cross-encoder
    pairs = [
        (query, chunk.get("chunk_text", chunk.get("text", "")))
        for chunk in chunks
    ]

    # Batch tokenize all pairs
    inputs = _reranker_tokenizer(
        pairs,
        max_length=512,
        padding=True,
        truncation=True,
        return_tensors="pt",
    )

    # Batch inference — single forward pass for all pairs
    with torch.no_grad():
        scores = _reranker_model(**inputs).logits.squeeze(-1)

    # If single input, scores may be 0-dim tensor
    if scores.dim() == 0:
        scores = scores.unsqueeze(0)

    # Attach scores to chunks
    scored_chunks = []
    for i, chunk in enumerate(chunks):
        scored_chunk = {**chunk, "_rerank_score": float(scores[i])}
        scored_chunks.append(scored_chunk)

    # Sort by reranker score descending
    scored_chunks.sort(key=lambda x: x["_rerank_score"], reverse=True)

    return scored_chunks[:top_k]


# =============================================================================
# 11. RESULT PARSER — Convert OpenSearch response to chunk dicts
# =============================================================================

def _parse_search_results(response: dict) -> list[dict]:
    """
    Parse OpenSearch search response into a list of chunk dicts.

    Each dict contains all fields from the OpenSearch document _source,
    matching the schema in opensearch_index_mapping.json and the fields
    consumed by the CRAG state machine's generate() node:
      - chunk_id, chunk_text, hierarchy_path, title
      - article_num, paragraph_num, effective_date
      - doc_id, security_classification, source_url
      - ecli_id, parent_chunk_id, sub_paragraph, etc.

    Also preserves the OpenSearch _score for downstream use (RRF, logging).
    """
    hits = response.get("hits", {}).get("hits", [])
    results = []

    for hit in hits:
        source = hit.get("_source", {})
        chunk = {
            # ── Core identification ──
            "chunk_id": source.get("chunk_id", ""),
            "doc_id": source.get("doc_id", ""),
            "doc_type": source.get("doc_type", ""),

            # ── Content ──
            "chunk_text": source.get("chunk_text", ""),
            "title": source.get("title", ""),

            # ── Legal hierarchy (critical for citation reconstruction) ──
            "hierarchy_path": source.get("hierarchy_path", ""),
            "article_num": source.get("article_num"),
            "paragraph_num": source.get("paragraph_num"),
            "sub_paragraph": source.get("sub_paragraph"),
            "chapter": source.get("chapter"),
            "section": source.get("section"),

            # ── Temporal versioning ──
            "effective_date": source.get("effective_date"),
            "expiry_date": source.get("expiry_date"),
            "version": source.get("version"),

            # ── Security ──
            "security_classification": source.get("security_classification", "PUBLIC"),

            # ── References ──
            "source_url": source.get("source_url"),
            "parent_chunk_id": source.get("parent_chunk_id"),
            "ecli_id": source.get("ecli_id"),
            "language": source.get("language", "nl"),

            # ── Search metadata ──
            "_score": hit.get("_score", 0.0),
        }
        results.append(chunk)

    return results


# =============================================================================
# 12. WORKED EXAMPLE — Full retrieval pipeline trace
# =============================================================================

"""
WORKED EXAMPLE: "Wat is de arbeidskorting voor 2024?"
(Simple factual query about the 2024 employment tax credit)

Step 1: Query Classification (module3_crag_statemachine.py)
  → query_type = SIMPLE (no ECLI/Article reference detected)
  → should_use_hyde = True (conceptual question without legal references)

Step 2: HyDE Transform (module3_crag_statemachine.py)
  → HyDE generates: "Op grond van artikel 3.114 Wet IB 2001 bedraagt de
     arbeidskorting voor het kalenderjaar 2024 maximaal 5.532 euro..."
  → This hypothetical text is used as the retrieval query

Step 3: Hybrid Retrieval (THIS MODULE)

  BM25 Results (top-5 of 20, using dutch_legal_analyzer):
  ┌──────┬──────────────────────────────────────────────────────┬───────────┐
  │ Rank │ chunk_id                                             │ BM25 Score│
  ├──────┼──────────────────────────────────────────────────────┼───────────┤
  │  1   │ WetIB2001-2024::art3.114::lid1::chunk001            │ 24.7      │
  │  2   │ WetIB2001-2024::art3.114::lid2::chunk001            │ 22.1      │
  │  3   │ WetIB2001-2024::art8.10::lid1::chunk001             │ 18.3      │
  │  4   │ Handboek-Loonbelasting-2024::ch7::sec3::chunk002    │ 16.5      │
  │  5   │ WetIB2001-2024::art3.114::lid3::chunk001            │ 15.9      │
  └──────┴──────────────────────────────────────────────────────┴───────────┘
  (Article 3.114 ranks high because BM25 matches "arbeidskorting" exactly)

  kNN Results (top-5 of 20, cosine similarity):
  ┌──────┬──────────────────────────────────────────────────────┬───────────┐
  │ Rank │ chunk_id                                             │ Cosine    │
  ├──────┼──────────────────────────────────────────────────────┼───────────┤
  │  1   │ Handboek-Loonbelasting-2024::ch7::sec3::chunk002    │ 0.847     │
  │  2   │ WetIB2001-2024::art8.10::lid1::chunk001             │ 0.832     │
  │  3   │ WetIB2001-2024::art3.114::lid1::chunk001            │ 0.829     │
  │  4   │ Belastingdienst-FAQ-2024::arbeidskorting::chunk001   │ 0.821     │
  │  5   │ WetIB2001-2024::art3.114::lid2::chunk001            │ 0.815     │
  └──────┴──────────────────────────────────────────────────────┴───────────┘
  (The handbook and FAQ rank higher semantically because they explain the concept)

  RRF Fusion (k=60, top-5 shown):
  ┌──────┬──────────────────────────────────────────────────────┬───────────┐
  │ Rank │ chunk_id                                             │ RRF Score │
  ├──────┼──────────────────────────────────────────────────────┼───────────┤
  │  1   │ WetIB2001-2024::art3.114::lid1::chunk001            │ 0.0327    │
  │      │   BM25 rank=1: 1/(60+1) + kNN rank=3: 1/(60+3)     │           │
  │  2   │ WetIB2001-2024::art3.114::lid2::chunk001            │ 0.0315    │
  │      │   BM25 rank=2: 1/(60+2) + kNN rank=5: 1/(60+5)     │           │
  │  3   │ Handboek-Loonbelasting-2024::ch7::sec3::chunk002    │ 0.0312    │
  │      │   BM25 rank=4: 1/(60+4) + kNN rank=1: 1/(60+1)     │           │
  │  4   │ WetIB2001-2024::art8.10::lid1::chunk001             │ 0.0307    │
  │      │   BM25 rank=3: 1/(60+3) + kNN rank=2: 1/(60+2)     │           │
  │  5   │ Belastingdienst-FAQ-2024::arbeidskorting::chunk001   │ 0.0164    │
  │      │   BM25 rank=n/a + kNN rank=4: 1/(60+4)             │           │
  └──────┴──────────────────────────────────────────────────────┴───────────┘
  Note: Article 3.114 lid 1 wins because it ranks high in BOTH lists.
  The FAQ only appeared in kNN (conceptual match, no BM25 keyword overlap).

Step 4: Cross-Encoder Reranking (all 40 → top 8)
  Reranker confirms Article 3.114 lid 1 as most relevant (score: 0.94).
  Reorders remaining chunks based on query-passage joint attention.
  Top-8 passed to the CRAG grader → generation pipeline.

Step 5: Latency Trace
  embed_query():           28ms
  _bm25_retrieve():        19ms  ┐
  _knn_retrieve():         76ms  ┘ parallel → 76ms
  _rrf_fuse():              3ms
  rerank_chunks():        187ms
  ─────────────────────────────
  Total retrieval+rerank: 294ms  (budget: 315ms ✓)
"""
