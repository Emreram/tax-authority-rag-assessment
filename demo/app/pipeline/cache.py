"""
Semantic cache with security-tier partitioning.
Key format: cache:{tier}:{sha256(normalized_query)[:16]}
Tier partitioning ensures CLASSIFIED_FIOD responses never reach PUBLIC users.
"""

import hashlib
import json
import time
from redis import Redis
from app.models import SecurityTier
from app.config import get_settings
from app.security.rbac import TIER_ACCESS
import structlog

log = structlog.get_logger()


def _normalize_query(query: str) -> str:
    return " ".join(query.lower().strip().split())


def _cache_key(tier: SecurityTier, query: str) -> str:
    normalized = _normalize_query(query)
    qhash = hashlib.sha256(normalized.encode()).hexdigest()[:16]
    return f"cache:{tier.value}:{qhash}"


def check_cache(redis_client: Redis, query: str, security_tier: SecurityTier) -> dict | None:
    """
    Check cache for a matching response.
    Users can only read entries from tiers they have access to (tier partitioning).
    """
    t0 = time.time()
    accessible_tiers = TIER_ACCESS.get(security_tier, ["PUBLIC"])

    for tier_name in accessible_tiers:
        tier = SecurityTier(tier_name)
        key = _cache_key(tier, query)
        raw = redis_client.get(key)
        if raw:
            entry = json.loads(raw)
            ms = (time.time() - t0) * 1000
            log.info("cache_hit", tier=tier_name, ms=round(ms, 1))
            return entry

    log.info("cache_miss")
    return None


def store_cache(
    redis_client: Redis,
    query: str,
    security_tier: SecurityTier,
    response: str,
    citations: list[dict],
    doc_ids: list[str],
    query_type: str = "SIMPLE",
) -> None:
    settings = get_settings()
    key = _cache_key(security_tier, query)

    entry = {
        "response": response,
        "citations": citations,
        "doc_ids": doc_ids,
        "security_tier": security_tier.value,
        "query_type": query_type,
        "cached_at": time.time(),
    }

    # TTL strategy: procedural queries cached longer, default 24h
    procedural_keywords = ["procedure", "aanvraag", "formulier", "indienen", "termijn", "bezwaar"]
    if any(kw in query.lower() for kw in procedural_keywords):
        ttl = settings.cache_ttl_procedural
    else:
        ttl = settings.cache_ttl_default

    redis_client.setex(key, ttl, json.dumps(entry))
    log.info("cache_stored", tier=security_tier.value, ttl=ttl)


def get_cache_stats(redis_client: Redis) -> dict:
    stats = {"total_entries": 0, "entries_by_tier": {}, "keys": []}
    for tier in SecurityTier:
        pattern = f"cache:{tier.value}:*"
        keys = list(redis_client.scan_iter(match=pattern))
        stats["entries_by_tier"][tier.value] = len(keys)
        stats["total_entries"] += len(keys)
    return stats
