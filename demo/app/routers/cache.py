import json
import time

from fastapi import APIRouter, Request
from app.config import get_settings
from app.models import SecurityTier
from app.pipeline import embedder as _embedder
from app.pipeline.cache import _cosine, get_cache_stats
from app.security.rbac import TIER_ACCESS
import structlog

log = structlog.get_logger()
router = APIRouter()


@router.get("/cache/stats", summary="Cache statistics by security tier")
async def cache_stats(request: Request):
    stats = get_cache_stats(request.app.state.redis)
    return stats


@router.get("/metrics/summary", summary="Reliability counters (cache hits, refuse rates, …)")
async def metrics_summary(request: Request):
    """Surfaces metric:* counters from Redis for the Kwaliteit-tab reliability cards."""
    from app.metrics import get_summary
    counters = get_summary(request.app.state.redis)
    hits = counters.get("cache_hits", 0)
    misses = counters.get("cache_misses", 0)
    total = hits + misses
    return {
        "counters": counters,
        "rates": {
            "cache_hit_ratio": (hits / total) if total else None,
            "total_queries": total,
        },
    }


@router.get("/audit/recent", summary="Recent query audit-trail (last 50)")
async def audit_recent(request: Request, limit: int = 50):
    """M10 — per-query audit log. Used by the Operations → Toegang dashboard."""
    from app.audit import list_recent
    rows = list_recent(request.app.state.redis, limit=max(1, min(limit, 200)))
    return {"entries": rows, "count": len(rows)}


@router.get("/cache/entries", summary="List all cache entries with optional similarity scoring")
async def cache_entries(request: Request, query: str | None = None, tier: str = "PUBLIC"):
    """Returns one row per cached entry. If ?query= is provided, also returns cosine
    similarity between that query's embedding and each cached entry's embedding."""
    settings = get_settings()
    redis = request.app.state.redis
    try:
        user_tier = SecurityTier(tier)
    except ValueError:
        user_tier = SecurityTier.PUBLIC
    accessible = set(TIER_ACCESS.get(user_tier, ["PUBLIC"]))

    qemb = None
    if query:
        try:
            qemb = await _embedder.embed_query(query)
        except Exception as e:
            log.warning("cache_entries_embed_failed", error=str(e))

    entries: list[dict] = []
    for key in redis.scan_iter(match="cache:*", count=200):
        parts = key.split(":")
        if len(parts) < 2:
            continue
        entry_tier = parts[1]
        raw = redis.get(key)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        sim = None
        if qemb and data.get("query_embedding"):
            sim = round(_cosine(qemb, data["query_embedding"]), 4)
        entries.append({
            "key": key,
            "tier": entry_tier,
            "accessible_to_user": entry_tier in accessible,
            "query": data.get("query"),
            "query_type": data.get("query_type"),
            "cached_at": data.get("cached_at"),
            "response_preview": (data.get("response") or "")[:200],
            "citation_count": len(data.get("citations", [])),
            "similarity_to_probe": sim,
            "would_hit": (sim is not None and sim >= settings.cache_similarity_threshold and entry_tier in accessible),
        })
    entries.sort(key=lambda e: (e["similarity_to_probe"] or 0), reverse=True)
    return {
        "threshold": settings.cache_similarity_threshold,
        "user_tier": user_tier.value,
        "accessible_tiers": sorted(accessible),
        "probe_query": query,
        "entries": entries,
    }


@router.delete("/cache/clear", summary="Clear all cache entries (admin)")
async def cache_clear(request: Request):
    redis = request.app.state.redis
    keys = list(redis.scan_iter(match="cache:*"))
    if keys:
        redis.delete(*keys)
    return {"cleared": len(keys)}
