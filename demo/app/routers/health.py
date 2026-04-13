from fastapi import APIRouter, Request
from app.config import get_settings
import structlog

log = structlog.get_logger()
router = APIRouter()


@router.get("/health", summary="Simple health check")
async def health():
    return {"status": "healthy"}


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

    status["config"] = {
        "llm_model": settings.gemini_llm_model,
        "embedding_model": settings.gemini_embedding_model,
        "embedding_dim": settings.embedding_dim,
        "cache_threshold": settings.cache_similarity_threshold,
        "max_retries": settings.max_retries,
        "security_tiers": ["PUBLIC", "INTERNAL", "RESTRICTED", "CLASSIFIED_FIOD"],
    }

    return status


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
