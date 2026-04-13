from fastapi import APIRouter, Request
from app.pipeline.cache import get_cache_stats
import structlog

log = structlog.get_logger()
router = APIRouter()


@router.get("/cache/stats", summary="Cache statistics by security tier")
async def cache_stats(request: Request):
    stats = get_cache_stats(request.app.state.redis)
    return stats


@router.delete("/cache/clear", summary="Clear all cache entries (admin)")
async def cache_clear(request: Request):
    redis = request.app.state.redis
    keys = list(redis.scan_iter(match="cache:*"))
    if keys:
        redis.delete(*keys)
    return {"cleared": len(keys)}
