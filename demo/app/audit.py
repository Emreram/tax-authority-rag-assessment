"""Per-query audit-trail backed by Redis sorted-sets, one per UTC day.

Every query (success, refuse, breaker-trip, cache-hit) gets one entry. The
sorted-set is keyed by timestamp so we can retrieve the most recent N
records cheaply for the Operations → Toegang dashboard.

Sync Redis client to match the rest of the demo (memory.py, cache.py).
"""
from __future__ import annotations

import json
import time

import structlog
from redis import Redis

log = structlog.get_logger()

_RETENTION_DAYS = 7
_MAX_QUERY_LEN = 500
_MAX_CITATIONS = 10


def _day_key(ts: float) -> str:
    return "audit:" + time.strftime("%Y-%m-%d", time.gmtime(ts))


def log_query(
    redis_client: Redis,
    *,
    session_id: str,
    tier: str,
    query: str,
    grade: str | None,
    citations: list[str],
    ttft_ms: float | None,
    source: str,
) -> None:
    ts = time.time()
    record = {
        "ts": ts,
        "session_id": session_id,
        "tier": tier,
        "query": (query or "")[:_MAX_QUERY_LEN],
        "grade": grade or "",
        "citations": (citations or [])[:_MAX_CITATIONS],
        "ttft_ms": ttft_ms,
        "source": source,
    }
    key = _day_key(ts)
    try:
        redis_client.zadd(key, {json.dumps(record, default=str): ts})
        redis_client.expire(key, _RETENTION_DAYS * 24 * 3600)
    except Exception as e:
        # Audit failures must never break user-facing flow.
        log.warning("audit_log_failed", error=str(e))


def list_recent(redis_client: Redis, *, day: str | None = None, limit: int = 50) -> list[dict]:
    day = day or time.strftime("%Y-%m-%d", time.gmtime())
    key = "audit:" + day
    try:
        rows = redis_client.zrevrange(key, 0, limit - 1)
    except Exception as e:
        log.warning("audit_list_failed", error=str(e))
        return []
    out: list[dict] = []
    for r in rows:
        try:
            out.append(json.loads(r if isinstance(r, str) else r.decode("utf-8")))
        except Exception:
            continue
    return out
