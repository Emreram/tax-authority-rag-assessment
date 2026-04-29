"""
Semantic cache with security-tier partitioning.

Instead of SHA256 hashing queries (exact match only), we store an embedding per
cached entry and look up by cosine similarity ≥ 0.97. This means
'Wat is de arbeidskorting 2024?' and 'arbeidskorting 2024 wat is dat?' both hit
the same cache entry.

Tier partitioning: a user can only read entries from tiers they have access to.
We iterate per-tier, score, and take the best hit above threshold.
"""

from __future__ import annotations

import hashlib
import json
import math
import time

from redis import Redis

from app.config import get_settings
from app.models import SecurityTier
from app.security.rbac import TIER_ACCESS

import structlog

log = structlog.get_logger()


def _normalize(query: str) -> str:
    return " ".join(query.lower().strip().split())


def _primary_key(tier: SecurityTier, query: str) -> str:
    """Hash key kept for fast exact-match wins before the vector search."""
    qhash = hashlib.sha256(_normalize(query).encode()).hexdigest()[:16]
    return f"cache:{tier.value}:{qhash}"


def _index_key(tier: SecurityTier) -> str:
    """Redis key of the list that tracks all cache entries for a tier."""
    return f"cache_idx:{tier.value}"


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if not na or not nb:
        return 0.0
    return dot / (na * nb)


def _serialize(entry: dict) -> str:
    return json.dumps(entry, default=str)


def _deserialize(raw: str) -> dict:
    try:
        return json.loads(raw)
    except Exception:
        return {}


async def check_cache_semantic(
    redis_client: Redis,
    query: str,
    security_tier: SecurityTier,
    embedder,  # module: app.pipeline.embedder (avoid circular import at module load)
) -> dict | None:
    """
    Semantic cache lookup.
    1) fast path: exact-hash match in own tier.
    2) slow path: score every entry in accessible tiers by cosine(query_emb, entry.query_emb).
    """
    settings = get_settings()
    threshold = settings.cache_similarity_threshold

    # Fast path: exact-hash hit in own tier (saves an embedding call).
    # Cache is on the hot path of every chat-query — fail-soft on Redis errors so a
    # transient hiccup degrades to a cache MISS rather than a 500.
    try:
        raw = redis_client.get(_primary_key(security_tier, query))
    except Exception as e:
        log.warning("cache_get_failed", error=str(e))
        return None
    if raw:
        entry = _deserialize(raw)
        if entry:
            entry["_match"] = "exact"
            log.info("cache_hit_exact", tier=security_tier.value)
            return entry

    # Slow path: embed query once, scan accessible tiers.
    try:
        query_emb = await embedder.embed_query(query)
    except Exception as e:
        log.warning("cache_embed_failed", error=str(e))
        return None

    best: tuple[float, dict] | None = None
    accessible = TIER_ACCESS.get(security_tier, ["PUBLIC"])
    try:
        for tier_name in accessible:
            tier = SecurityTier(tier_name)
            for key in redis_client.scan_iter(match=f"cache:{tier.value}:*", count=100):
                raw = redis_client.get(key)
                if not raw:
                    continue
                entry = _deserialize(raw)
                emb = entry.get("query_embedding") or []
                if not emb:
                    continue
                score = _cosine(query_emb, emb)
                if score >= threshold and (best is None or score > best[0]):
                    best = (score, entry)
    except Exception as e:
        log.warning("cache_scan_failed", error=str(e))
        return None
    if best:
        score, entry = best
        entry["_match"] = "semantic"
        entry["_similarity"] = round(score, 4)
        log.info("cache_hit_semantic", tier=security_tier.value, score=round(score, 3))
        return entry

    log.info("cache_miss")
    return None


# Backwards-compatible sync wrapper (non-semantic) for legacy callers.
def check_cache(redis_client: Redis, query: str, security_tier: SecurityTier) -> dict | None:
    accessible = TIER_ACCESS.get(security_tier, ["PUBLIC"])
    for tier_name in accessible:
        tier = SecurityTier(tier_name)
        raw = redis_client.get(_primary_key(tier, query))
        if raw:
            entry = _deserialize(raw)
            if entry:
                entry["_match"] = "exact"
                return entry
    return None


async def store_cache_semantic(
    redis_client: Redis,
    query: str,
    security_tier: SecurityTier,
    response: str,
    citations: list[dict],
    doc_ids: list[str],
    embedder,
    query_type: str = "SIMPLE",
) -> None:
    settings = get_settings()
    try:
        emb = await embedder.embed_query(query)
    except Exception as e:
        log.warning("cache_store_embed_failed", error=str(e))
        emb = []

    entry = {
        "response": response,
        "citations": citations,
        "doc_ids": doc_ids,
        "security_tier": security_tier.value,
        "query": query,
        "query_type": query_type,
        "query_embedding": emb,
        "cached_at": time.time(),
    }

    procedural_keywords = ["procedure", "aanvraag", "formulier", "indienen", "termijn", "bezwaar"]
    ttl = settings.cache_ttl_procedural if any(k in query.lower() for k in procedural_keywords) else settings.cache_ttl_default

    try:
        redis_client.setex(_primary_key(security_tier, query), ttl, _serialize(entry))
    except Exception as e:
        log.warning("cache_setex_failed", error=str(e))


# Legacy sync wrapper (hash-only).
def store_cache(redis_client, query, security_tier, response, citations, doc_ids, query_type="SIMPLE"):
    settings = get_settings()
    entry = {
        "response": response,
        "citations": citations,
        "doc_ids": doc_ids,
        "security_tier": security_tier.value,
        "query": query,
        "query_type": query_type,
        "cached_at": time.time(),
    }
    ttl = settings.cache_ttl_default
    redis_client.setex(_primary_key(security_tier, query), ttl, _serialize(entry))


def get_cache_stats(redis_client: Redis) -> dict:
    stats = {"total_entries": 0, "entries_by_tier": {}, "keys": []}
    try:
        for tier in SecurityTier:
            pattern = f"cache:{tier.value}:*"
            keys = list(redis_client.scan_iter(match=pattern))
            stats["entries_by_tier"][tier.value] = len(keys)
            stats["total_entries"] += len(keys)
    except Exception as e:
        log.warning("cache_stats_failed", error=str(e))
    return stats
