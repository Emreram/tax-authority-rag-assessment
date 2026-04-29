"""Reliability counters in Redis. Best-effort — never break user flow on failure."""
from __future__ import annotations

import structlog
from redis import Redis

log = structlog.get_logger()

_PREFIX = "metric:"


def incr(redis_client: Redis, name: str, by: int = 1) -> None:
    try:
        redis_client.incrby(_PREFIX + name, by)
    except Exception as e:
        log.warning("metric_incr_failed", name=name, error=str(e))


def get_summary(redis_client: Redis) -> dict:
    """Return all counters under metric:* as a flat dict."""
    try:
        keys = list(redis_client.scan_iter(match=_PREFIX + "*", count=200))
        if not keys:
            return {}
        values = redis_client.mget(keys)
        return {
            k.removeprefix(_PREFIX) if isinstance(k, str) else k.decode("utf-8").removeprefix(_PREFIX): int(v or 0)
            for k, v in zip(keys, values)
        }
    except Exception as e:
        log.warning("metric_summary_failed", error=str(e))
        return {}
