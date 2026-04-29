"""Diagnose WHY a refuse occurred.

When the CRAG pipeline refuses, we want to tell the user WHY — not the same
generic message regardless of cause. This module runs a single diagnostic
OS-search WITHOUT the RBAC filter and compares to the user's tier so we can
distinguish three categories:

  - CORPUS_GAP        — the index has no relevant content (we genuinely don't know)
  - TIER_GAP          — content exists but in a higher tier than the user has
  - SEMANTIC_MISMATCH — content exists in the user's tier but the grader rejected it

The extra search is on the refuse-path only (already slow, +200ms is invisible).
Fail-soft: any OS error → SEMANTIC_MISMATCH so the existing refuse still ships.
"""
from __future__ import annotations

import structlog

from app.models import SecurityTier
from app.security.rbac import TIER_HIERARCHY

log = structlog.get_logger()

# Score thresholds for e5 cosine. e5 baseline is ~0.75 for ANY two pieces of NL text,
# so we need stricter cutoffs to distinguish noise from real matches.
_CORPUS_GAP_CUTOFF = 0.80   # top-1 below this → no real match anywhere → CORPUS_GAP
_PLAUSIBLE_CUTOFF = 0.78    # below this we treat a hit as noise for tier-determination


async def classify_refuse(
    os_client,
    query: str,
    query_embedding: list[float],
    user_tier: SecurityTier,
    settings,
) -> dict:
    """Return {"category", "detail", "higher_tier_needed", "corpus_match_count"}."""
    fallback = {
        "category": "SEMANTIC_MISMATCH",
        "detail": "diagnostiek niet beschikbaar",
        "higher_tier_needed": None,
        "corpus_match_count": 0,
    }

    if not query_embedding:
        return fallback

    # Diagnostic kNN with NO RBAC filter — we want to see all matches across all tiers.
    body = {
        "size": 5,
        "query": {
            "knn": {"embedding": {"vector": query_embedding, "k": 5}},
        },
        "_source": ["chunk_id", "security_classification", "hierarchy_path"],
    }
    try:
        resp = os_client.search(index=settings.opensearch_index, body=body)
    except Exception as e:
        log.warning("refuse_classify_search_failed", error=str(e))
        return fallback

    hits = resp.get("hits", {}).get("hits", [])
    if not hits:
        return {
            "category": "CORPUS_GAP",
            "detail": "geen enkel document in het corpus matcht deze query",
            "higher_tier_needed": None,
            "corpus_match_count": 0,
        }

    top_score = hits[0].get("_score", 0)
    user_level = TIER_HIERARCHY.get(user_tier, 0)

    # Step 1: top-1 score is the truth-teller for "is there ANY real match?"
    if top_score < _CORPUS_GAP_CUTOFF:
        return {
            "category": "CORPUS_GAP",
            "detail": f"beste match scoort {top_score:.2f} (drempel {_CORPUS_GAP_CUTOFF}) — geen onderwerp in corpus",
            "higher_tier_needed": None,
            "corpus_match_count": 0,
        }

    # Step 2: there IS a real match. Where does it live?
    # Find the highest-scoring hit ABOVE the user's tier (if any), and the highest
    # plausible hit AT OR BELOW the user's tier.
    best_above_tier: tuple[float, str] | None = None  # (score, tier_name)
    best_in_tier_score = 0.0
    plausible_above_count = 0
    for h in hits:
        score = h.get("_score", 0)
        if score < _PLAUSIBLE_CUTOFF:
            continue
        cls = h["_source"].get("security_classification", "PUBLIC")
        try:
            hit_tier = SecurityTier(cls)
        except ValueError:
            continue
        hit_level = TIER_HIERARCHY.get(hit_tier, 0)
        if hit_level > user_level:
            plausible_above_count += 1
            if best_above_tier is None or score > best_above_tier[0]:
                best_above_tier = (score, cls)
        else:
            if score > best_in_tier_score:
                best_in_tier_score = score

    # TIER_GAP: a hit above user's tier scores BETTER than anything in user's tier.
    # Means: the actually-relevant content lives in a higher classification.
    if best_above_tier and best_above_tier[0] > best_in_tier_score:
        return {
            "category": "TIER_GAP",
            "detail": f"beste match staat in {best_above_tier[1]} (score {best_above_tier[0]:.2f}) — {plausible_above_count} hit(s) boven jouw tier",
            "higher_tier_needed": best_above_tier[1],
            "corpus_match_count": plausible_above_count,
        }

    # SEMANTIC_MISMATCH: there are plausible matches in the user's tier but grader
    # didn't accept any. Either query phrasing or grader strictness.
    return {
        "category": "SEMANTIC_MISMATCH",
        "detail": f"plausibele kandidaten in jouw tier (top-score {best_in_tier_score:.2f}) maar grader gaf geen RELEVANT",
        "higher_tier_needed": None,
        "corpus_match_count": int(best_in_tier_score >= _PLAUSIBLE_CUTOFF),
    }
