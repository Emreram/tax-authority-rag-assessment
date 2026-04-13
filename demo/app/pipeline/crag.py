"""
CRAG State Machine — imperative Python implementation.

States: CLASSIFY → TRANSFORM → RETRIEVE → GRADE
  → RELEVANT  → GENERATE → VALIDATE → RESPOND
  → AMBIGUOUS (retry<MAX_RETRIES) → REWRITE → RETRIEVE → GRADE → ...
  → IRRELEVANT / AMBIGUOUS-exhausted → REFUSE

Every step appends to pipeline_trace for full observability.
"""

import time
import uuid
from dataclasses import dataclass, field
from typing import Optional
from opensearchpy import OpenSearch
from redis import Redis
from app.config import get_settings
from app.models import SecurityTier, Citation, PipelineStep, QueryResponse, TimingBreakdown
from app.pipeline.classifier import classify_query
from app.pipeline.retriever import retrieve
from app.pipeline.grader import grade_context
from app.pipeline.generator import generate_response, rewrite_query
from app.pipeline.validator import validate_citations
from app.pipeline.cache import check_cache, store_cache
import structlog

log = structlog.get_logger()


@dataclass
class CRAGState:
    query: str
    security_tier: SecurityTier
    session_id: str
    query_type: Optional[str] = None
    retrieved_chunks: list = field(default_factory=list)
    graded_chunks: list = field(default_factory=list)
    grading_result: Optional[str] = None
    response_text: Optional[str] = None
    cited_ids: list = field(default_factory=list)
    citations_valid: bool = False
    retry_count: int = 0
    pipeline_trace: list = field(default_factory=list)
    timings: dict = field(default_factory=dict)

    def add_step(self, node: str, result: str = None, detail: str = None, duration_ms: float = 0.0):
        self.pipeline_trace.append(PipelineStep(
            node=node, result=result, detail=detail, duration_ms=round(duration_ms, 1)
        ))


REFUSE_NO_CONTEXT = (
    "Op basis van de beschikbare documentatie kan ik uw vraag niet beantwoorden. "
    "De opgehaalde passages zijn niet voldoende relevant voor uw specifieke vraag. "
    "Probeer uw vraag te herformuleren of raadpleeg een belastingadviseur."
)

REFUSE_INVALID_CITATIONS = (
    "Er is een antwoord gegenereerd, maar de broncitaties konden niet worden geverifieerd. "
    "Om nauwkeurigheid te garanderen wordt dit antwoord niet weergegeven. "
    "Probeer uw vraag opnieuw."
)


async def run_crag(
    query: str,
    security_tier: SecurityTier,
    session_id: str,
    os_client: OpenSearch,
    redis_client: Redis,
) -> QueryResponse:
    settings = get_settings()
    t_total = time.time()
    state = CRAGState(query=query, security_tier=security_tier, session_id=session_id)

    # ── CACHE CHECK ──────────────────────────────────────────────────────────
    t0 = time.time()
    cached = check_cache(redis_client, query, security_tier)
    cache_ms = (time.time() - t0) * 1000
    if cached:
        state.add_step("cache_lookup", result="HIT", detail="Served from cache", duration_ms=cache_ms)
        return QueryResponse(
            response=cached["response"],
            citations=[Citation(**c) for c in cached["citations"]],
            source="cache",
            pipeline_trace=state.pipeline_trace,
            timing=TimingBreakdown(total_ms=cache_ms, cache_ms=cache_ms),
            session_id=session_id,
        )
    state.add_step("cache_lookup", result="MISS", duration_ms=cache_ms)

    # ── CLASSIFY ─────────────────────────────────────────────────────────────
    t0 = time.time()
    state.query_type = await classify_query(query)
    classify_ms = (time.time() - t0) * 1000
    state.add_step("classify_query", result=state.query_type, duration_ms=classify_ms)
    log.info("classified", type=state.query_type)

    # ── RETRIEVE + GRADE LOOP (max MAX_RETRIES retries) ───────────────────────
    current_query = query
    retrieve_ms = 0.0
    grade_ms = 0.0

    while True:
        # RETRIEVE
        t0 = time.time()
        state.retrieved_chunks = await retrieve(
            os_client, current_query, security_tier, state.query_type, settings
        )
        _rm = (time.time() - t0) * 1000
        retrieve_ms += _rm
        state.add_step(
            "retrieve",
            result=f"{len(state.retrieved_chunks)} chunks",
            detail=f"RRF fusion of BM25+kNN, tier={security_tier.value}",
            duration_ms=_rm,
        )

        # GRADE
        t0 = time.time()
        grading = await grade_context(query, state.retrieved_chunks, settings)
        _gm = (time.time() - t0) * 1000
        grade_ms += _gm
        state.grading_result = grading["overall"]
        state.graded_chunks = grading["relevant_chunks"] or state.retrieved_chunks[:4]
        state.add_step(
            "grade_context",
            result=state.grading_result,
            detail=f"Relevant: {len(grading['relevant_chunks'])} / {len(state.retrieved_chunks)}",
            duration_ms=_gm,
        )

        if state.grading_result == "RELEVANT":
            break

        if state.grading_result == "AMBIGUOUS" and state.retry_count < settings.max_retries:
            # REWRITE AND RETRY
            state.retry_count += 1
            t0 = time.time()
            current_query = await rewrite_query(query)
            rw_ms = (time.time() - t0) * 1000
            state.add_step(
                "rewrite_and_retry",
                result=f"retry {state.retry_count}/{settings.max_retries}",
                detail=f"Rewritten: {current_query[:80]}",
                duration_ms=rw_ms,
            )
            log.info("retrying", attempt=state.retry_count, rewritten=current_query[:60])
            continue

        # IRRELEVANT or AMBIGUOUS with retries exhausted → REFUSE
        state.add_step("refuse", result=state.grading_result, detail="Insufficient context")
        return QueryResponse(
            response=REFUSE_NO_CONTEXT,
            citations=[],
            source="pipeline",
            pipeline_trace=state.pipeline_trace,
            timing=TimingBreakdown(
                total_ms=(time.time() - t_total) * 1000,
                classification_ms=classify_ms,
                retrieval_ms=retrieve_ms,
                grading_ms=grade_ms,
            ),
            session_id=session_id,
            grading_result=state.grading_result,
            query_type=state.query_type,
        )

    # ── GENERATE ─────────────────────────────────────────────────────────────
    t0 = time.time()
    state.response_text, state.cited_ids = await generate_response(
        query, state.graded_chunks
    )
    gen_ms = (time.time() - t0) * 1000
    state.add_step(
        "generate",
        result=f"{len(state.cited_ids)} citations",
        detail=f"Generated with {len(state.graded_chunks)} context chunks",
        duration_ms=gen_ms,
    )

    # ── VALIDATE ─────────────────────────────────────────────────────────────
    validation = validate_citations(state.response_text, state.cited_ids, state.graded_chunks)
    state.citations_valid = validation["valid"]
    state.add_step(
        "validate_output",
        result="PASSED" if state.citations_valid else "FAILED",
        detail=validation["reason"],
        duration_ms=1.0,
    )

    if not state.citations_valid:
        state.add_step("refuse", result="INVALID_CITATIONS")
        return QueryResponse(
            response=REFUSE_INVALID_CITATIONS,
            citations=[],
            source="pipeline",
            pipeline_trace=state.pipeline_trace,
            timing=TimingBreakdown(
                total_ms=(time.time() - t_total) * 1000,
                classification_ms=classify_ms,
                retrieval_ms=retrieve_ms,
                grading_ms=grade_ms,
                generation_ms=gen_ms,
            ),
            session_id=session_id,
            grading_result=state.grading_result,
            query_type=state.query_type,
        )

    # ── RESPOND ──────────────────────────────────────────────────────────────
    citations = []
    for chunk in state.graded_chunks:
        if chunk["chunk_id"] in state.cited_ids:
            citations.append(Citation(
                chunk_id=chunk["chunk_id"],
                hierarchy_path=chunk.get("hierarchy_path", ""),
                title=chunk.get("title", ""),
                article_ref=chunk.get("article_num"),
                effective_date=chunk.get("effective_date"),
            ))

    total_ms = (time.time() - t_total) * 1000
    state.add_step("respond", result="SUCCESS", detail=f"{len(citations)} sources", duration_ms=1.0)

    response = QueryResponse(
        response=state.response_text,
        citations=citations,
        source="pipeline",
        pipeline_trace=state.pipeline_trace,
        timing=TimingBreakdown(
            total_ms=total_ms,
            classification_ms=classify_ms,
            retrieval_ms=retrieve_ms,
            grading_ms=grade_ms,
            generation_ms=gen_ms,
        ),
        session_id=session_id,
        grading_result=state.grading_result,
        query_type=state.query_type,
    )

    # Store in cache
    store_cache(
        redis_client,
        query,
        security_tier,
        state.response_text,
        [c.model_dump() for c in citations],
        list({c["doc_id"] for c in state.graded_chunks}),
        query_type=state.query_type or "SIMPLE",
    )

    return response
