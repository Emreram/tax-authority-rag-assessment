from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from app.config import get_settings
from app.pipeline.llm import ping as llm_ping
import structlog

log = structlog.get_logger()
router = APIRouter()


@router.get("/readyz", summary="Readiness — all dependencies up (LLM, OpenSearch, Redis)")
async def readyz(request: Request):
    """Distinct from /health (process alive). 503 until every dep pings ok."""
    settings = get_settings()
    deps = {}
    ready = True

    # LLM
    llm_ready = bool(getattr(request.app.state, "llm_ready", False))
    deps["llm"] = "ready" if llm_ready else "unreachable"
    if not llm_ready:
        ready = False

    # OpenSearch
    try:
        request.app.state.opensearch.cluster.health(request_timeout=2)
        deps["opensearch"] = "ready"
    except Exception as e:
        deps["opensearch"] = f"unreachable: {type(e).__name__}"
        ready = False

    # Redis
    try:
        request.app.state.redis.ping()
        deps["redis"] = "ready"
    except Exception as e:
        deps["redis"] = f"unreachable: {type(e).__name__}"
        ready = False

    payload = {"ready": ready, "deps": deps, "stage": getattr(request.app.state, "warmup_stage", "unknown")}
    return JSONResponse(payload, status_code=200 if ready else 503)


@router.get("/health", summary="Simple health check")
async def health(request: Request):
    warmup_complete = bool(getattr(request.app.state, "warmup_complete", False))
    return {
        "status": "healthy" if warmup_complete else "warming",
        "warmup_complete": warmup_complete,
        "warmup_stage": getattr(request.app.state, "warmup_stage", "unknown"),
    }


@router.get("/health/detailed", summary="Detailed service status")
async def health_detailed(request: Request):
    settings = get_settings()
    status = {"status": "healthy", "services": {}, "config": {}}

    # OpenSearch
    try:
        info = request.app.state.opensearch.cluster.health()
        count = request.app.state.opensearch.count(index=settings.opensearch_index)
        status["services"]["opensearch"] = {
            "status": "connected",
            "cluster_status": info["status"],
            "index_doc_count": count["count"],
            "index": settings.opensearch_index,
        }
    except Exception as e:
        status["services"]["opensearch"] = {"status": "error", "detail": str(e)}
        status["status"] = "degraded"

    # Redis
    try:
        request.app.state.redis.ping()
        from app.pipeline.cache import get_cache_stats
        stats = get_cache_stats(request.app.state.redis)
        status["services"]["redis"] = {
            "status": "connected",
            "cache_entries": stats["total_entries"],
            "entries_by_tier": stats["entries_by_tier"],
        }
    except Exception as e:
        status["services"]["redis"] = {"status": "error", "detail": str(e)}
        status["status"] = "degraded"

    # LLM — Docker Model Runner
    try:
        if await llm_ping():
            status["services"]["model_runner"] = {
                "status": "connected",
                "base_url": settings.llm_base_url,
                "model": settings.llm_model,
            }
        else:
            status["services"]["model_runner"] = {"status": "unreachable", "base_url": settings.llm_base_url}
            status["status"] = "degraded"
    except Exception as e:
        status["services"]["model_runner"] = {"status": "error", "detail": str(e)}
        status["status"] = "degraded"

    status["config"] = {
        "llm_model": settings.llm_model,
        "llm_base_url": settings.llm_base_url,
        "embedding_model": settings.embedding_model,
        "embedding_dim": settings.embedding_dim,
        "cache_threshold": settings.cache_similarity_threshold,
        "max_retries": settings.max_retries,
        "security_tiers": ["PUBLIC", "INTERNAL", "RESTRICTED", "CLASSIFIED_FIOD"],
    }

    return status


@router.get("/v1/admin/index_stats", summary="Index size + memory projection per quantization mode")
async def index_stats(request: Request):
    """Memory math for the live OpenSearch index across precisions.
    Used by the Operations → Ingestie quantization-widget. Numbers are
    deterministic (n × dim × bytes × HNSW-overhead); no quantization is
    actually applied at runtime — this is the projection panel for the
    production-scale story (assessment §Module 1).
    """
    settings = get_settings()
    try:
        n = request.app.state.opensearch.count(index=settings.opensearch_index)["count"]
    except Exception as e:
        return {"error": str(e), "chunks": 0}

    dim = settings.embedding_dim
    overhead = 1.8  # HNSW graph overhead factor (m=16 connections + ef=128 neighbours)

    # Bytes per vector at each precision (× HNSW overhead)
    bytes_per_vec = {"fp32": dim * 4, "fp16": dim * 2, "int8": dim * 1, "pq8": dim * 0.125}
    memory_now = {k: int(n * v * overhead) for k, v in bytes_per_vec.items()}
    memory_20m = {k: int(20_000_000 * v * overhead) for k, v in bytes_per_vec.items()}

    return {
        "chunks": n,
        "dim": dim,
        "overhead": overhead,
        "current_precision": "fp32",  # OpenSearch default for this demo
        "memory_bytes": memory_now,
        "production_20m_bytes": memory_20m,
        "production_target_chunks": 20_000_000,
    }


@router.get("/v1/corpus/version", summary="Corpus version stamp — newest ingest timestamp + counts")
async def corpus_version(request: Request):
    """Surfaces a 'corpus version' for the UI refuse-bubble: when was the most
    recent doc ingested, how many docs/chunks, and per-tier breakdown.
    """
    settings = get_settings()
    os = request.app.state.opensearch
    try:
        body = {
            "size": 0,
            "aggs": {
                "newest": {"max": {"field": "ingestion_timestamp"}},
                "by_tier": {"terms": {"field": "security_classification", "size": 10}},
                "doc_count": {"cardinality": {"field": "doc_id"}},
            },
        }
        resp = os.search(index=settings.opensearch_index, body=body)
        aggs = resp.get("aggregations", {})
        total_chunks = resp["hits"]["total"]["value"]
        newest_ms = aggs.get("newest", {}).get("value")
        per_tier = {b["key"]: b["doc_count"] for b in aggs.get("by_tier", {}).get("buckets", [])}
        return {
            "corpus_version": (aggs.get("newest", {}).get("value_as_string") or "unknown")[:10],
            "newest_ingest_epoch_ms": newest_ms,
            "doc_count": int(aggs.get("doc_count", {}).get("value", 0)),
            "chunk_count": total_chunks,
            "chunks_per_tier": per_tier,
        }
    except Exception as e:
        return {"error": str(e), "corpus_version": "unknown"}


@router.get("/health/pipeline", summary="Pipeline architecture info")
async def pipeline_info():
    return {
        "states": [
            "cache_lookup",
            "classify_query",
            "retrieve",
            "grade_context",
            "rewrite_and_retry",
            "generate",
            "validate_output",
            "respond",
            "refuse",
        ],
        "transitions": {
            "RELEVANT": "generate → validate_output → respond",
            "AMBIGUOUS (retry<1)": "rewrite_and_retry → retrieve → grade_context",
            "IRRELEVANT or AMBIGUOUS-exhausted": "refuse",
            "INVALID_CITATIONS": "refuse",
        },
        "max_retries": 1,
        "rrf_k": 60,
        "description": "CRAG state machine — grade-then-generate with citation validation",
    }
