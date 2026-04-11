"""
Module 4 (Part 1): Semantic Cache — Role-Partitioned Query Caching with Redis
==============================================================================

This module answers the assessment questions:
  "Design and implement a semantic caching layer."
  "How do you prevent serving stale or cross-tier cached responses?"
  "What is your cache invalidation strategy for legal document updates?"

Design principles:
  1. Conservative threshold (0.97 cosine) — near-misses are catastrophic in fiscal domain.
  2. Role-partitioned — a CLASSIFIED_FIOD response is never served to a helpdesk user.
  3. TTL-based expiry — different TTLs for different query types.
  4. Document-aware invalidation — when a document is re-indexed, all cache entries
     referencing that document's chunks are invalidated.
  5. Cache wraps the CRAG pipeline — check BEFORE the state machine, store AFTER.

Stack: Redis Stack 7.x with RediSearch (vector similarity search).

Latency:
  Cache check (hit):   ~10-15ms (Redis in-memory vector search)
  Cache check (miss):  ~10-15ms (same, just no result above threshold)
  Cache store:          ~5ms   (write + vector index update)

The cache sits BEFORE the CRAG state machine:
  Query → Cache Check → HIT?  → Return cached response (TTFT ≈ 15ms)
                       → MISS? → Run full CRAG pipeline → Store result → Return
"""

import hashlib
import json
import re
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

import numpy as np
from pydantic import BaseModel, Field
from redis import Redis
from redis.commands.search.field import VectorField, TagField, TextField, NumericField
from redis.commands.search.indexDefinition import IndexDefinition, IndexType
from redis.commands.search.query import Query


# =============================================================================
# 1. CONSTANTS — Cache configuration
# =============================================================================

CACHE_SIMILARITY_THRESHOLD = 0.97
"""
Minimum cosine similarity for a cache hit.

Why 0.97 and not lower:
  "Wat is het Box 1 tarief voor 2024?" and "Wat is het Box 1 tarief voor 2023?"
  have ~0.94 cosine similarity (same structure, one word different).
  At threshold 0.94: cache HIT → serves 2023 rate for a 2024 question.
  At threshold 0.97: cache MISS → triggers fresh retrieval → correct 2024 rate.

  In fiscal domain, serving the wrong year's tax rate is a critical error that
  could lead to incorrect tax assessments. We accept more cache misses (lower
  hit rate) in exchange for guaranteed accuracy.

Also configured in rbac_roles.json cache_partitioning and the master plan Section 3 Decision #10.
"""

DEFAULT_TTL_SECONDS = 86_400       # 24 hours
PROCEDURAL_TTL_SECONDS = 604_800   # 7 days
CASE_LAW_TTL_SECONDS = 0           # No cache (0 = skip caching entirely)

"""
TTL strategy rationale:
  - Default (24h): Tax rates, thresholds, and legal provisions can change annually.
    24h is conservative enough to catch most updates within a news cycle.
  - Procedural (7d): "How do I file a return?" or "What forms do I need?" change
    infrequently. 7-day cache reduces load for FAQ-heavy helpdesk queries.
  - Case law (0 = no cache): Court rulings can be overturned or clarified by newer
    rulings. Caching "what does ECLI:NL:HR:2023:1234 say" is dangerous if a higher
    court later reverses the ruling. Better to always retrieve fresh.
"""

CACHE_INDEX_NAME = "tax_rag_cache"
EMBEDDING_DIM = 1024
CACHE_PREFIX = "cache:"


# =============================================================================
# 2. SECURITY TIER HIERARCHY — For cache lookup permissions
# =============================================================================

class SecurityTier(str, Enum):
    """
    Security tiers in ascending access order.
    Must match the 4 tiers in rbac_roles.json and module1_ingestion.py.
    """
    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    RESTRICTED = "RESTRICTED"
    CLASSIFIED_FIOD = "CLASSIFIED_FIOD"


TIER_HIERARCHY = [
    SecurityTier.PUBLIC,
    SecurityTier.INTERNAL,
    SecurityTier.RESTRICTED,
    SecurityTier.CLASSIFIED_FIOD,
]
"""
Ordered from least to most privileged.

A user at tier T can access cache entries tagged with tier <= T:
  - Helpdesk (INTERNAL): can hit PUBLIC, INTERNAL cache entries
  - Tax Inspector (RESTRICTED): can hit PUBLIC, INTERNAL, RESTRICTED
  - FIOD Investigator (CLASSIFIED_FIOD): can hit all tiers
  - Public user (PUBLIC): can only hit PUBLIC entries

This mirrors the DLS role hierarchy in rbac_roles.json.
"""


def get_accessible_tiers(user_tier: str) -> list[str]:
    """
    Return all cache tiers a user is allowed to access.

    A user with RESTRICTED access can read cache entries created from
    PUBLIC, INTERNAL, or RESTRICTED context — but NOT CLASSIFIED_FIOD.

    Why this matters:
      A FIOD investigator asks "transfer pricing investigation procedures".
      The response is cached with tier=CLASSIFIED_FIOD (because it used
      classified source documents). If a helpdesk user later asks the same
      question, the cache lookup must NOT return the FIOD-tier entry,
      because it may contain information from classified documents.
    """
    try:
        user_index = TIER_HIERARCHY.index(SecurityTier(user_tier))
    except (ValueError, KeyError):
        user_index = 0  # Default to PUBLIC (most restrictive)

    return [tier.value for tier in TIER_HIERARCHY[: user_index + 1]]


# =============================================================================
# 3. CACHE ENTRY MODEL
# =============================================================================

class CacheEntry(BaseModel):
    """
    A single cached query-response pair.

    Stores everything needed to serve a cached response without re-running
    the CRAG pipeline, plus metadata for invalidation and security.
    """
    query_text: str = Field(
        description="Original query text (for debugging and audit logging)"
    )
    query_embedding: list[float] = Field(
        description="1024-dim embedding of the query (for similarity matching)"
    )
    response_text: str = Field(
        description="The generated response text (with inline citations)"
    )
    citations: list[dict] = Field(
        default_factory=list,
        description="Citation list from the CRAG pipeline's validate_output step"
    )
    retrieved_doc_ids: list[str] = Field(
        default_factory=list,
        description=(
            "doc_ids of chunks used to generate this response. "
            "Used for invalidation: when any of these docs are re-indexed, "
            "this cache entry is invalidated."
        )
    )
    security_tier: str = Field(
        description=(
            "The security tier of the user who triggered this cache entry. "
            "Determines which tier of documents contributed to the response. "
            "Cache lookup filters by tier to prevent cross-tier contamination."
        )
    )
    query_type: str = Field(
        default="SIMPLE",
        description="Query classification (SIMPLE, COMPLEX, REFERENCE) for TTL selection"
    )
    created_at: str = Field(
        default_factory=lambda: datetime.utcnow().isoformat(),
        description="ISO timestamp of cache creation"
    )
    ttl_seconds: int = Field(
        default=DEFAULT_TTL_SECONDS,
        description="Time-to-live in seconds for this cache entry"
    )


# =============================================================================
# 4. TTL STRATEGY — Query-type-aware expiration
# =============================================================================

# Patterns for classifying query type (for TTL selection)
_CASE_LAW_PATTERNS = [
    re.compile(r"ECLI:NL:", re.IGNORECASE),
    re.compile(r"jurisprudentie|uitspraak|arrest|vonnis", re.IGNORECASE),
]

_PROCEDURAL_PATTERNS = [
    re.compile(r"procedure|aanvraag|formulier|indienen|aangifte\s+doen", re.IGNORECASE),
    re.compile(r"hoe\s+(kan|moet|doe)\s+ik", re.IGNORECASE),
]


def determine_ttl(query_text: str, query_type: str = "SIMPLE") -> int:
    """
    Select the appropriate cache TTL based on query content.

    Rules:
      1. Case law queries → 0 (no cache)
         New rulings can overturn old ones. A cached interpretation may become
         outdated when a higher court rules differently.

      2. Procedural/FAQ queries → 7 days
         "How do I file my tax return?" changes infrequently.
         Helpdesk users ask these repeatedly — high cache value.

      3. Everything else → 24 hours
         Tax rates, thresholds, legal interpretations. Conservative default.

    Note: TTL is a MAXIMUM lifetime. Document-aware invalidation (Section 7)
    can evict entries earlier if their source documents are re-indexed.
    """
    # Case law: no caching
    if query_type == "REFERENCE":
        for pattern in _CASE_LAW_PATTERNS:
            if pattern.search(query_text):
                return CASE_LAW_TTL_SECONDS

    # Procedural/FAQ: extended TTL
    for pattern in _PROCEDURAL_PATTERNS:
        if pattern.search(query_text):
            return PROCEDURAL_TTL_SECONDS

    # Default
    return DEFAULT_TTL_SECONDS


# =============================================================================
# 5. SEMANTIC CACHE — Core implementation
# =============================================================================

class SemanticCache:
    """
    Redis-backed semantic cache with vector similarity search.

    Architecture:
      - Redis Stack with RediSearch module provides in-memory vector indexing
      - Each cache entry is stored as a Redis Hash with an associated vector
      - Cache lookups use KNN vector search with pre-filtering by security tier
      - Cosine similarity threshold of 0.97 prevents near-miss contamination

    Cache key format: cache:{security_tier}:{embedding_hash}
    (matches rbac_roles.json cache_partitioning.key_format)
    """

    def __init__(
        self,
        redis_client: Optional[Redis] = None,
        host: str = "localhost",
        port: int = 6379,
    ):
        self.redis = redis_client or Redis(host=host, port=port, decode_responses=False)
        self._ensure_index()

    def _ensure_index(self) -> None:
        """
        Create the RediSearch vector index if it doesn't exist.

        Index schema:
          - embedding: FLOAT32 vector (1024-dim, HNSW, COSINE) — for similarity search
          - security_tier: TAG — for pre-filtered tier-based lookups
          - query_text: TEXT — for debugging and audit
          - created_at: NUMERIC — for TTL-based expiration queries
          - doc_ids: TAG — for document-aware invalidation queries
        """
        try:
            self.redis.ft(CACHE_INDEX_NAME).info()
            return  # Index already exists
        except Exception:
            pass  # Index doesn't exist, create it

        schema = (
            VectorField(
                "embedding",
                "HNSW",
                {
                    "TYPE": "FLOAT32",
                    "DIM": EMBEDDING_DIM,
                    "DISTANCE_METRIC": "COSINE",
                    "M": 16,
                    "EF_CONSTRUCTION": 200,
                },
            ),
            TagField("security_tier"),
            TextField("query_text"),
            NumericField("created_at_ts"),
            TagField("doc_ids", separator="|"),
        )

        definition = IndexDefinition(
            prefix=[CACHE_PREFIX],
            index_type=IndexType.HASH,
        )

        self.redis.ft(CACHE_INDEX_NAME).create_index(
            schema, definition=definition
        )

    # ─────────────────────────────────────────────────
    # CACHE LOOKUP
    # ─────────────────────────────────────────────────

    def check_cache(
        self,
        query_text: str,
        query_embedding: list[float],
        user_security_tier: str,
    ) -> Optional[CacheEntry]:
        """
        Check if a semantically similar query has been cached.

        Steps:
          1. Determine which tiers the user can access
          2. Run KNN vector search (K=1) with tier pre-filter
          3. Check if the best match exceeds the 0.97 cosine threshold
          4. If hit: return the cached response (TTFT ≈ 15ms)
          5. If miss: return None (proceed to full CRAG pipeline)

        Security:
          A helpdesk user (INTERNAL tier) can only hit cache entries with
          security_tier IN (PUBLIC, INTERNAL). Even if a nearly identical
          query was cached by a FIOD investigator, the tier filter excludes it.

        Parameters:
          query_text: The user's query (for logging)
          query_embedding: Pre-computed 1024-dim embedding of the query
          user_security_tier: User's security tier for cache partition filtering

        Returns:
          CacheEntry if a sufficiently similar cached query is found, else None.
        """
        accessible_tiers = get_accessible_tiers(user_security_tier)
        tier_filter = " | ".join(accessible_tiers)

        # Convert embedding to bytes for Redis
        embedding_bytes = np.array(query_embedding, dtype=np.float32).tobytes()

        # RediSearch KNN query with tier pre-filter
        # @security_tier:{PUBLIC | INTERNAL} filters BEFORE vector search
        query = (
            Query(f"(@security_tier:{{{tier_filter}}})=>[KNN 1 @embedding $vec AS score]")
            .sort_by("score")
            .return_fields("score", "query_text", "response_text", "citations_json",
                           "doc_ids", "security_tier", "query_type", "created_at_ts",
                           "ttl_seconds")
            .dialect(2)
        )

        result = self.redis.ft(CACHE_INDEX_NAME).search(
            query, query_params={"vec": embedding_bytes}
        )

        if not result.docs:
            return None

        best_match = result.docs[0]

        # RediSearch COSINE distance = 1 - cosine_similarity
        # So similarity = 1 - distance
        distance = float(best_match.score)
        similarity = 1.0 - distance

        # ── Threshold check ──
        if similarity < CACHE_SIMILARITY_THRESHOLD:
            # Near-miss: similar but not similar enough.
            # This is the critical safety check.
            # "Box 1 rate 2024" vs "Box 1 rate 2023" → similarity ~0.94 → MISS
            return None

        # ── Cache HIT — reconstruct CacheEntry ──
        return CacheEntry(
            query_text=best_match.query_text.decode() if isinstance(best_match.query_text, bytes) else best_match.query_text,
            query_embedding=query_embedding,  # Use the current embedding
            response_text=best_match.response_text.decode() if isinstance(best_match.response_text, bytes) else best_match.response_text,
            citations=json.loads(best_match.citations_json) if best_match.citations_json else [],
            retrieved_doc_ids=best_match.doc_ids.decode().split("|") if best_match.doc_ids else [],
            security_tier=best_match.security_tier.decode() if isinstance(best_match.security_tier, bytes) else best_match.security_tier,
            query_type=best_match.query_type.decode() if isinstance(best_match.query_type, bytes) else str(best_match.query_type),
            created_at=datetime.fromtimestamp(float(best_match.created_at_ts)).isoformat(),
            ttl_seconds=int(best_match.ttl_seconds),
        )

    # ─────────────────────────────────────────────────
    # CACHE STORE
    # ─────────────────────────────────────────────────

    def store_cache(
        self,
        query_text: str,
        query_embedding: list[float],
        response_text: str,
        citations: list[dict],
        retrieved_doc_ids: list[str],
        security_tier: str,
        query_type: str = "SIMPLE",
    ) -> Optional[str]:
        """
        Store a successful CRAG pipeline response in the cache.

        Only called after the full CRAG pipeline completes successfully
        (state machine reached RESPOND, not REFUSE).

        Steps:
          1. Determine TTL based on query type
          2. If TTL == 0 (case law), skip caching entirely
          3. Build cache key: cache:{security_tier}:{embedding_hash}
          4. Store as Redis Hash with vector embedding and metadata
          5. Set Redis TTL for automatic expiration

        Parameters:
          query_text: Original user query
          query_embedding: 1024-dim embedding
          response_text: Generated response with inline citations
          citations: Citation list from validate_output
          retrieved_doc_ids: doc_ids of source documents (for invalidation)
          security_tier: User's security tier (partition key)
          query_type: SIMPLE, COMPLEX, or REFERENCE

        Returns:
          Cache key if stored, None if skipped (TTL=0 or error).
        """
        ttl = determine_ttl(query_text, query_type)

        # Case law: do not cache
        if ttl == 0:
            return None

        # Build deterministic cache key
        embedding_hash = hashlib.sha256(
            np.array(query_embedding, dtype=np.float32).tobytes()
        ).hexdigest()[:16]

        cache_key = f"{CACHE_PREFIX}{security_tier}:{embedding_hash}"

        # Prepare Redis Hash fields
        embedding_bytes = np.array(query_embedding, dtype=np.float32).tobytes()
        now = datetime.utcnow()

        mapping = {
            "embedding": embedding_bytes,
            "query_text": query_text,
            "response_text": response_text,
            "citations_json": json.dumps(citations),
            "doc_ids": "|".join(retrieved_doc_ids),
            "security_tier": security_tier,
            "query_type": query_type,
            "created_at_ts": now.timestamp(),
            "ttl_seconds": ttl,
        }

        # Store with TTL
        self.redis.hset(cache_key, mapping=mapping)
        self.redis.expire(cache_key, ttl)

        return cache_key

    # ─────────────────────────────────────────────────
    # CACHE INVALIDATION — Document-aware
    # ─────────────────────────────────────────────────

    def invalidate_by_doc_ids(self, doc_ids: list[str]) -> int:
        """
        Invalidate all cache entries that reference any of the given doc_ids.

        Called when documents are re-indexed (e.g., new legislation version,
        amended ruling). Any cached response that was generated using chunks
        from these documents may now be stale and must be evicted.

        Example:
          1. AWR-2024-v3 (new amendment to Algemene wet rijksbelastingen) is ingested
          2. This function is called with doc_ids=["AWR-2024-v3"]
          3. All cache entries whose retrieved_doc_ids include "AWR-2024-v3" are deleted
          4. Next query about AWR triggers fresh retrieval → picks up the amended text

        Uses RediSearch TAG query on the doc_ids field.

        Returns:
          Number of cache entries invalidated.
        """
        invalidated_count = 0

        for doc_id in doc_ids:
            # Search for cache entries referencing this doc_id
            query = Query(f"@doc_ids:{{{doc_id}}}").no_content()
            result = self.redis.ft(CACHE_INDEX_NAME).search(query)

            for doc in result.docs:
                self.redis.delete(doc.id)
                invalidated_count += 1

        return invalidated_count

    def invalidate_by_tier(self, security_tier: str) -> int:
        """
        Invalidate ALL cache entries for a specific security tier.

        Use case: emergency invalidation if a security incident is detected
        (e.g., a document was temporarily accessible at the wrong tier).

        Returns:
          Number of cache entries invalidated.
        """
        query = Query(f"@security_tier:{{{security_tier}}}").no_content()
        result = self.redis.ft(CACHE_INDEX_NAME).search(query)

        count = 0
        for doc in result.docs:
            self.redis.delete(doc.id)
            count += 1

        return count

    def get_cache_stats(self) -> dict:
        """
        Return cache statistics for monitoring (exposed via Prometheus metrics).

        Tracked metrics:
          - total_entries: Current cache size
          - entries_by_tier: Distribution across security tiers
          - index_info: RediSearch index health

        These feed into the Grafana dashboard for cache observability.
        """
        info = self.redis.ft(CACHE_INDEX_NAME).info()
        total = int(info.get("num_docs", 0))

        # Count entries per tier
        tier_counts = {}
        for tier in SecurityTier:
            query = Query(f"@security_tier:{{{tier.value}}}").no_content().paging(0, 0)
            result = self.redis.ft(CACHE_INDEX_NAME).search(query)
            tier_counts[tier.value] = result.total

        return {
            "total_entries": total,
            "entries_by_tier": tier_counts,
            "index_size_mb": float(info.get("inverted_sz_mb", 0)),
        }


# =============================================================================
# 6. PIPELINE INTEGRATION — Cache wraps the CRAG state machine
# =============================================================================

def handle_query(
    query: str,
    user_security_tier: str,
    session_id: str,
    semantic_cache: SemanticCache,
) -> dict:
    """
    Top-level query handler with cache-first strategy.

    This function sits ABOVE the CRAG state machine. It checks the cache
    BEFORE invoking the full pipeline, and stores successful responses AFTER.

    Why cache is outside the state machine (not a LangGraph node):
      - A cache hit should skip the ENTIRE pipeline — no state machine overhead
      - If cache were a node, a miss would still pay the graph traversal cost
      - Separation of concerns: caching is infrastructure, CRAG is logic

    Flow:
      1. Embed the query (needed for both cache check and potential retrieval)
      2. Check semantic cache (filtered by user's security tier)
      3. If HIT → return cached response immediately (TTFT ≈ 15ms)
      4. If MISS → invoke full CRAG state machine
      5. If CRAG succeeds → store response in cache for future hits
      6. Return response to user

    Parameters:
      query: User's question
      user_security_tier: One of PUBLIC, INTERNAL, RESTRICTED, CLASSIFIED_FIOD
      session_id: For audit logging and trace correlation
      semantic_cache: Initialized SemanticCache instance
    """
    from module2_retrieval import embed_query
    from module3_crag_statemachine import invoke_crag

    # ── Step 1: Embed query (shared between cache check and retrieval) ──
    query_embedding = embed_query(query)

    # ── Step 2: Check cache ──
    cached = semantic_cache.check_cache(
        query_text=query,
        query_embedding=query_embedding,
        user_security_tier=user_security_tier,
    )

    if cached:
        # CACHE HIT — return immediately without running the pipeline
        return {
            "response": cached.response_text,
            "citations": cached.citations,
            "source": "cache",
            "cache_similarity": "≥0.97",  # We know it passed the threshold
            "session_id": session_id,
        }

    # ── Step 3: Cache MISS — run full CRAG pipeline ──
    crag_result = invoke_crag(
        query=query,
        user_security_tier=user_security_tier,
        session_id=session_id,
    )

    final_response = crag_result.get("final_response")
    final_citations = crag_result.get("final_citations", [])

    # ── Step 4: Store successful responses in cache ──
    if final_response:
        # Extract doc_ids from retrieved chunks for invalidation tracking
        retrieved_doc_ids = list({
            chunk.get("doc_id", "")
            for chunk in crag_result.get("graded_chunks", [])
            if chunk.get("doc_id")
        })

        semantic_cache.store_cache(
            query_text=query,
            query_embedding=query_embedding,
            response_text=final_response,
            citations=final_citations,
            retrieved_doc_ids=retrieved_doc_ids,
            security_tier=user_security_tier,
            query_type=crag_result.get("query_type", "SIMPLE"),
        )

    return {
        "response": final_response or crag_result.get("error_message", "Unable to answer."),
        "citations": final_citations,
        "source": "crag_pipeline",
        "pipeline_trace": crag_result.get("pipeline_trace", []),
        "session_id": session_id,
    }


# =============================================================================
# 7. DOCUMENT-AWARE INVALIDATION — Triggered by ingestion pipeline
# =============================================================================

def on_documents_reindexed(
    doc_ids: list[str],
    semantic_cache: SemanticCache,
) -> dict:
    """
    Callback invoked by the ingestion pipeline (module1_ingestion.py)
    after documents are re-indexed.

    When legislation is amended or a new court ruling is published,
    the ingestion pipeline re-processes the affected documents.
    This callback ensures cached responses based on the OLD version
    are invalidated immediately.

    Example flow:
      1. Wet IB 2001 is amended (new Box 1 rates for 2025)
      2. Ingestion pipeline re-indexes all AWR-2025 chunks
      3. This function is called: on_documents_reindexed(["AWR-2025-v1"])
      4. All cache entries that used AWR-2025 chunks are deleted
      5. Next query about 2025 rates triggers fresh retrieval
      6. Fresh retrieval picks up the amended rates

    Without this mechanism, cached responses would serve STALE rates
    until their TTL expires — potentially 24 hours of incorrect answers.

    Returns:
      Summary dict with invalidation count (for audit logging).
    """
    invalidated = semantic_cache.invalidate_by_doc_ids(doc_ids)

    return {
        "action": "cache_invalidation",
        "trigger": "document_reindex",
        "doc_ids": doc_ids,
        "invalidated_entries": invalidated,
        "timestamp": datetime.utcnow().isoformat(),
    }


# =============================================================================
# 8. WORKED EXAMPLES
# =============================================================================

"""
EXAMPLE 1: Cache HIT — Repeat FAQ query
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  09:00 — Helpdesk user A asks: "Wat is het BTW-tarief?"
    → Cache: MISS (empty cache)
    → CRAG pipeline: retrieves Article 9 Wet OB, generates response
    → Cache STORE: key = cache:INTERNAL:a3f8b2c1...
                   tier = INTERNAL
                   ttl = 86400 (24h)
                   doc_ids = ["WetOB-2024"]

  09:15 — Helpdesk user B asks: "Wat is het BTW tarief?"  (no hyphen)
    → Embed query → cosine similarity to cached query = 0.986
    → 0.986 ≥ 0.97 → Cache HIT
    → Return cached response
    → TTFT: 12ms (vs ~1200ms for full pipeline)


EXAMPLE 2: Cache MISS — Year confusion prevention
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  10:00 — User asks: "Wat is het Box 1 tarief voor 2024?"
    → CRAG pipeline → cached with tier=RESTRICTED

  10:30 — User asks: "Wat is het Box 1 tarief voor 2023?"
    → Embed query → cosine similarity to "...2024?" = 0.941
    → 0.941 < 0.97 → Cache MISS → correct behavior!
    → Full CRAG pipeline runs → retrieves the 2023 rate (different!)
    → Separate cache entry created

  Why this matters: The 2024 Box 1 rate is 36.97%, the 2023 rate was 36.93%.
  A cache hit would serve the WRONG rate. The 0.97 threshold prevents this.


EXAMPLE 3: Cross-tier cache BLOCK
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  11:00 — FIOD investigator asks: "fraude-onderzoek transfer pricing"
    → CRAG pipeline retrieves CLASSIFIED_FIOD documents
    → Cached with tier=CLASSIFIED_FIOD

  11:15 — Helpdesk user asks: "transfer pricing onderzoek" (similar query)
    → Embed query → cosine similarity to FIOD user's query = 0.982
    → 0.982 ≥ 0.97 (would be a hit on similarity alone)
    → BUT: tier filter = {PUBLIC, INTERNAL} (helpdesk access)
    → Cached entry has tier=CLASSIFIED_FIOD → EXCLUDED from search
    → Cache MISS → helpdesk user gets answer from PUBLIC+INTERNAL docs only
    → No classified information leakage ✓


EXAMPLE 4: Document-aware invalidation
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Day 1: Multiple queries about AWR Article 67 → responses cached
         Cache entries reference doc_id="AWR-2024-v2"

  Day 2: AWR is amended (v3). Ingestion pipeline re-indexes AWR-2024-v3.
         on_documents_reindexed(["AWR-2024-v2"]) is called.
         → 7 cache entries referencing AWR-2024-v2 are deleted

  Day 2 (later): User asks about AWR Article 67
         → Cache MISS (entry was invalidated)
         → Fresh retrieval picks up the v3 amendment
         → New response reflects the updated law ✓

  Without invalidation: cached responses would serve the v2 text
  until TTL expiry (up to 24h of incorrect legal information).
"""
