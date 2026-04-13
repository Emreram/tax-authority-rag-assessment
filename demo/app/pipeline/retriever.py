"""
Hybrid retrieval: BM25 (sparse) + kNN (dense) with RRF fusion (k=60).
Pre-retrieval RBAC filter applied before scoring — mirrors OpenSearch DLS.
"""

import re
from opensearchpy import OpenSearch
from app.config import get_settings
from app.pipeline.llm import embed_text
from app.security.rbac import build_rbac_filter
from app.models import SecurityTier
import structlog

log = structlog.get_logger()

ECLI_PATTERN = re.compile(r"ECLI:[A-Z]{2}:[A-Z]{1,10}:\d{4}:[A-Z0-9]+", re.IGNORECASE)
ARTICLE_PATTERN = re.compile(r"\b[Aa]rt(?:ikel)?\s*\.?\s*(\d+[\.\:]?\d*[a-z]?)\b")

TEMPORAL_FILTER = {
    "bool": {
        "should": [
            {"bool": {"must_not": {"exists": {"field": "expiry_date"}}}},
            {"range": {"expiry_date": {"gt": "now"}}}
        ],
        "minimum_should_match": 1
    }
}


def _rrf_fuse(bm25_hits: list[dict], knn_hits: list[dict], k: int = 60) -> list[dict]:
    """Reciprocal Rank Fusion — rank-based, no score normalization needed."""
    scores: dict[str, float] = {}
    docs: dict[str, dict] = {}

    for rank, hit in enumerate(bm25_hits):
        cid = hit["_source"]["chunk_id"]
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
        docs[cid] = hit["_source"]

    for rank, hit in enumerate(knn_hits):
        cid = hit["_source"]["chunk_id"]
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
        docs[cid] = hit["_source"]

    sorted_ids = sorted(scores, key=lambda x: scores[x], reverse=True)
    return [{"chunk_id": cid, **docs[cid], "_rrf_score": scores[cid]} for cid in sorted_ids]


def _exact_id_search(
    client: OpenSearch, query: str, security_tier: SecurityTier, settings
) -> list[dict]:
    rbac_filter = build_rbac_filter(security_tier)
    ecli_match = ECLI_PATTERN.search(query)
    art_match = ARTICLE_PATTERN.search(query)

    if ecli_match:
        field, value = "ecli_id", ecli_match.group(0)
    elif art_match:
        field, value = "article_num", art_match.group(1)
    else:
        return []

    body = {
        "query": {
            "bool": {
                "must": [{"term": {field: value}}],
                "filter": [rbac_filter, TEMPORAL_FILTER]
            }
        },
        "size": settings.top_k_rerank,
    }
    resp = client.search(index=settings.opensearch_index, body=body)
    return resp["hits"]["hits"]


async def retrieve(
    client: OpenSearch,
    query: str,
    security_tier: SecurityTier,
    query_type: str,
    settings,
) -> list[dict]:
    """Main retrieval entry point — returns up to top_k_rerank chunks."""
    rbac_filter = build_rbac_filter(security_tier)

    # Exact-ID shortcut for REFERENCE queries
    if query_type == "REFERENCE":
        exact = _exact_id_search(client, query, security_tier, settings)
        if exact:
            log.info("exact_id_hit", count=len(exact))
            return [h["_source"] for h in exact]

    # Embed the query
    query_embedding = await embed_text(query, task_type="RETRIEVAL_QUERY")

    # BM25 search
    bm25_body = {
        "query": {
            "bool": {
                "must": [
                    {"multi_match": {
                        "query": query,
                        "fields": ["chunk_text^2", "title^1.5", "hierarchy_path"],
                        "analyzer": "dutch_legal_analyzer",
                        "type": "best_fields"
                    }}
                ],
                "filter": [rbac_filter, TEMPORAL_FILTER]
            }
        },
        "size": settings.top_k_bm25,
    }

    # kNN search
    knn_body = {
        "query": {
            "bool": {
                "must": [
                    {"knn": {
                        "embedding": {
                            "vector": query_embedding,
                            "k": settings.top_k_knn,
                        }
                    }}
                ],
                "filter": [rbac_filter, TEMPORAL_FILTER]
            }
        },
        "size": settings.top_k_knn,
    }

    bm25_resp = client.search(index=settings.opensearch_index, body=bm25_body)
    knn_resp = client.search(index=settings.opensearch_index, body=knn_body)

    bm25_hits = bm25_resp["hits"]["hits"]
    knn_hits = knn_resp["hits"]["hits"]

    log.info("retrieval_complete", bm25=len(bm25_hits), knn=len(knn_hits))

    fused = _rrf_fuse(
        bm25_hits, knn_hits, k=settings.rrf_rank_constant
    )

    return fused[:settings.top_k_rerank]
