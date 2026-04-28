import re
import time
import uuid

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, Field

from app.config import get_settings
from app.models import QueryRequest, QueryResponse, SecurityTier
from app.pipeline.crag import run_crag
from app.pipeline.embedder import embed_query
from app.pipeline.retriever import _rrf_fuse, _exact_id_search, TEMPORAL_FILTER
from app.security.rbac import build_rbac_filter
import structlog

log = structlog.get_logger()
router = APIRouter()


class TraceRequest(BaseModel):
    query: str = Field(..., min_length=1)
    security_tier: SecurityTier = SecurityTier.PUBLIC
    with_rerank: bool = False


@router.post("/retrieval/trace", summary="One-shot retrieval trace for the Retrieval workspace")
async def retrieval_trace(request: Request, body: TraceRequest):
    """Returns every stage of retrieval (BM25 / kNN / RRF / optional rerank) with scores.
    Powers the 'two rivers' visualization — no generation, no grading, no caching."""
    settings = get_settings()
    os_client = request.app.state.opensearch
    rbac = build_rbac_filter(body.security_tier)

    t0 = time.time()
    qvec = await embed_query(body.query)
    t_embed = (time.time() - t0) * 1000

    bm25_body = {
        "query": {
            "bool": {
                "must": [{
                    "multi_match": {
                        "query": body.query,
                        "fields": ["chunk_text^2", "title^1.5", "hierarchy_path"],
                        "analyzer": "dutch_legal_analyzer",
                        "type": "best_fields",
                    }
                }],
                "filter": [rbac, TEMPORAL_FILTER],
            }
        },
        "size": settings.top_k_bm25,
    }
    knn_body = {
        "query": {
            "bool": {
                "must": [{"knn": {"embedding": {"vector": qvec, "k": settings.top_k_knn}}}],
                "filter": [rbac, TEMPORAL_FILTER],
            }
        },
        "size": settings.top_k_knn,
    }

    t1 = time.time()
    bm25 = os_client.search(index=settings.opensearch_index, body=bm25_body)["hits"]["hits"]
    knn = os_client.search(index=settings.opensearch_index, body=knn_body)["hits"]["hits"]
    t_search = (time.time() - t1) * 1000

    def shape(h: dict, rank: int) -> dict:
        s = h["_source"]
        return {
            "rank": rank + 1,
            "chunk_id": s.get("chunk_id"),
            "hierarchy_path": s.get("hierarchy_path"),
            "security_classification": s.get("security_classification"),
            "score": round(h.get("_score", 0.0), 4),
            "preview": (s.get("chunk_text") or "")[:160],
        }

    bm25_shaped = [shape(h, i) for i, h in enumerate(bm25)]
    knn_shaped = [shape(h, i) for i, h in enumerate(knn)]

    fused = _rrf_fuse(bm25, knn, k=settings.rrf_rank_constant)
    fused_shaped = []
    for i, f in enumerate(fused):
        fused_shaped.append({
            "rank": i + 1,
            "chunk_id": f["chunk_id"],
            "hierarchy_path": f.get("hierarchy_path"),
            "rrf_score": round(f.get("_rrf_score", 0.0), 5),
            "preview": (f.get("chunk_text") or "")[:160],
        })

    reranked_shaped = []
    if body.with_rerank and fused:
        try:
            from app.pipeline.reranker import rerank as _rerank
            top = fused[: max(settings.top_k_rerank * 2, 20)]
            rr = await _rerank(body.query, top, top_k=settings.top_k_rerank)
            for i, r in enumerate(rr):
                reranked_shaped.append({
                    "rank": i + 1,
                    "chunk_id": r["chunk_id"],
                    "hierarchy_path": r.get("hierarchy_path"),
                    "rerank_score": round(float(r.get("_rerank_score", 0.0)), 4),
                    "preview": (r.get("chunk_text") or "")[:160],
                })
        except Exception as e:
            log.warning("trace_rerank_failed", error=str(e))

    return {
        "query": body.query,
        "security_tier": body.security_tier.value,
        "config": {
            "top_k_bm25": settings.top_k_bm25,
            "top_k_knn": settings.top_k_knn,
            "top_k_rerank": settings.top_k_rerank,
            "rrf_k": settings.rrf_rank_constant,
            "embedding_model": settings.embedding_model,
            "embedding_dim": settings.embedding_dim,
        },
        "timings_ms": {
            "embed": round(t_embed, 1),
            "search": round(t_search, 1),
            "total": round((time.time() - t0) * 1000, 1),
        },
        "bm25": bm25_shaped,
        "knn": knn_shaped,
        "fused": fused_shaped,
        "reranked": reranked_shaped,
    }


@router.post("/query", response_model=QueryResponse, summary="Run the CRAG pipeline on a tax query")
async def query_endpoint(request: Request, body: QueryRequest):
    session_id = body.session_id or str(uuid.uuid4())[:8]
    log.info("query_received", query=body.query[:80], tier=body.security_tier, session=session_id)

    try:
        result = await run_crag(
            query=body.query,
            security_tier=body.security_tier,
            session_id=session_id,
            os_client=request.app.state.opensearch,
            redis_client=request.app.state.redis,
        )
        return result
    except Exception as e:
        log.error("pipeline_error", error=str(e))
        raise HTTPException(status_code=500, detail=f"Pipeline error: {str(e)}")
