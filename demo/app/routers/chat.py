"""
Streaming chat endpoint.

POST /v1/chat emits Server-Sent Events over the CRAG pipeline:
  - event: trace       data: {node, result, detail, duration_ms}
  - event: chunk       data: {chunk_id, hierarchy_path, score, status}  # retrieved / relevant / cited
  - event: token       data: "<partial text>"
  - event: citation    data: Citation
  - event: parent_expansion  data: {parent_id, child_ids}
  - event: done        data: {session_id, timing, source}
  - event: error       data: {detail}
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import AsyncIterator

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app.config import get_settings
from app.models import SecurityTier
from app.pipeline import memory
from app.pipeline import embedder as _embedder
from app.audit import log_query as audit_log_query
from app.metrics import incr as metric_incr
from app.pipeline.cache import check_cache_semantic, store_cache_semantic
from app.pipeline.citation_format import compact_citations
from app.pipeline.classifier import classify_query, decompose_complex
from app.pipeline.generator import rewrite_query
from app.pipeline.grader import grade_context
from app.pipeline.llm import generate_stream, BreakerOpenError
from app.pipeline.breaker import breaker as _breaker
from app.pipeline.retriever import retrieve
from app.pipeline.validator import validate_citations

import structlog

log = structlog.get_logger()
router = APIRouter()


class ChatRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)
    security_tier: SecurityTier = SecurityTier.PUBLIC
    session_id: str | None = None


def _sse(event: str, data) -> dict:
    return {"event": event, "data": json.dumps(data, default=str)}


def _build_refuse_text(diag: dict, tier) -> str:
    """Map refuse-classifier output → user-facing refuse text. Compact, scannable."""
    cat = diag.get("category", "SEMANTIC_MISMATCH")
    if cat == "CORPUS_GAP":
        return (
            "Geen onderbouwd antwoord beschikbaar — **dit onderwerp zit nog niet in het corpus**.\n\n"
            "Meld de vraag aan de corpus-eigenaar (Operations → Documenten) "
            "zodat de relevante bron geïngest kan worden."
        )
    if cat == "TIER_GAP":
        higher = diag.get("higher_tier_needed") or "een hogere classificatie"
        return (
            f"Geen onderbouwd antwoord op jouw tier (**{tier.value}**) — "
            f"de relevante content staat op **{higher}**.\n\n"
            f"Vraag toegang tot {higher} aan, of leg de vraag voor aan een collega "
            f"met die classificatie."
        )
    return (
        "Geen onderbouwd antwoord — er zijn mogelijk gerelateerde documenten, "
        "maar geen die de vraag direct beantwoorden.\n\n"
        "Probeer een specifiekere formulering met wetsartikel of begrip "
        "(bijv. *art 3.14 Wet IB 2001*)."
    )


def _maybe_breaker_trace() -> dict | None:
    """If the breaker just transitioned, return an SSE-trace dict to surface it in the UI."""
    t = _breaker.consume_transition()
    if not t:
        return None
    old, new, _ts = t
    label = f"{old.value} → {new.value}"
    if new.value == "HALF_OPEN":
        detail = "trial request — breaker is testing recovery"
    elif new.value == "CLOSED" and old.value == "HALF_OPEN":
        detail = "RECOVERED — circuit closed after successful trial"
    elif new.value == "OPEN":
        detail = f"OPENED — {_breaker.threshold} failures in {_breaker.window_s:.0f}s"
    else:
        detail = label
    return _sse("trace", {"node": "breaker_state", "result": new.value, "detail": detail, "duration_ms": 0})


def _categorize_error(exc: Exception) -> tuple[str, str]:
    """Map exception → (category, friendly NL message). Never leak str(e) to client."""
    name = type(exc).__name__.lower()
    if "breakeropen" in name:
        return "LLM_UNAVAILABLE", "Inferentie tijdelijk overbelast — probeer het over enkele minuten opnieuw."
    if "timeout" in name or "readtimeout" in name:
        return "TIMEOUT", "De verwerking duurde te lang. Probeer het opnieuw of stel een specifiekere vraag."
    if "connection" in name or "connecterror" in name:
        return "INFRA_ERROR", "Een achterliggend systeem is tijdelijk niet bereikbaar."
    if "validation" in name or "pydantic" in name:
        return "VALIDATION_FAILED", "De vraag voldoet niet aan het verwachte formaat."
    return "INTERNAL", "Er ging intern iets mis. De fout is gelogd voor onderzoek."


GENERATOR_SYSTEM = """Je bent de KennisAssistent van de Belastingdienst.

INHOUDSREGELS:
1. Gebruik UITSLUITEND informatie uit de meegegeven contextpassages. Geen externe kennis.
2. Plaats na elke feitelijke claim één citatie in exact dit formaat: [Source: <chunk_id> | <hierarchy_path>]
3. Als de context onvoldoende is, zeg dat eerlijk in één korte zin.
4. Antwoord in het Nederlands tenzij de vraag in een andere taal staat.

OPMAAKREGELS (Markdown — voor leesbaarheid):
- Korte vraag (ja/nee, één feit) → 1–2 zinnen, geen heading, geen lijst.
- Numerieke schijven, tarieven of voorwaardenlijsten → gebruik een Markdown-tabel.
- Meerdere losse voorwaarden of stappen → gebruik bullet points (`- `).
- Bedragen, percentages en data: zet ze **vet** (`**€ 5.532**`, `**8,231%**`).
- GEEN 'Bronnen:' sectie aan het einde — de inline citaten zijn al voldoende, de UI toont de bronnen apart.
- Geen H1. Optioneel één H2 (`## Onderwerp`) als de vraag dat verdient (>3 punten).
- Houd de response compact — geen herhaling, geen disclaimer-tekst.
"""


async def _streaming_pipeline(
    request: Request,
    body: ChatRequest,
) -> AsyncIterator[dict]:
    settings = get_settings()
    session_id = body.session_id or str(uuid.uuid4())[:8]
    tier = body.security_tier
    os_client = request.app.state.opensearch
    redis_client = request.app.state.redis

    t_total = time.time()
    t_first_token: float | None = None  # M2: TTFT — set on first user-visible token

    # ─── memory / follow-up resolution ───
    resolved, original = await memory.resolve_followup(redis_client, session_id, body.query)
    if original:
        yield _sse("trace", {"node": "memory_resolve", "result": "REWRITTEN", "detail": f"{original[:40]!r} → {resolved[:60]!r}", "duration_ms": 0})
    query = resolved

    # ─── cache lookup (semantic) ───
    t0 = time.time()
    cached = await check_cache_semantic(redis_client, query, tier, _embedder)
    cache_ms = (time.time() - t0) * 1000
    if cached:
        detail = f"cache HIT ({cached.get('_match','?')}"
        if cached.get('_similarity'):
            detail += f", sim={cached['_similarity']}"
        detail += ")"
        yield _sse("trace", {"node": "cache_lookup", "result": "HIT", "detail": detail, "duration_ms": cache_ms})
        # M2: emit TTFT for cache-hit path before any tokens
        ttft_ms = (time.time() - t_total) * 1000
        yield _sse("ttft", {"ms": round(ttft_ms, 1), "source": "cache"})
        for piece in _split_for_stream(cached["response"]):
            yield _sse("token", piece)
            await asyncio.sleep(0.01)
        # Re-send the full cached text so the UI swaps the streamed plain-text for a
        # markdown-rendered version. Cached responses are already in compact [N] form.
        cached_order = [c["chunk_id"] for c in cached["citations"]]
        yield _sse("text_replace", {"text": cached["response"], "ref_order": cached_order})
        for c in cached["citations"]:
            yield _sse("citation", c)
        memory.append_turn(redis_client, session_id, body.query, cached["response"])
        # M10: audit log — cache-hit
        audit_log_query(
            redis_client, session_id=session_id, tier=tier.value, query=body.query,
            grade="CACHE_HIT", citations=[c.get("chunk_id", "") for c in cached.get("citations", [])],
            ttft_ms=ttft_ms, source="cache",
        )
        metric_incr(redis_client, "cache_hits")
        yield _sse("done", {"session_id": session_id, "source": "cache", "total_ms": (time.time() - t_total) * 1000})
        return
    metric_incr(redis_client, "cache_misses")
    yield _sse("trace", {"node": "cache_lookup", "result": "MISS", "duration_ms": cache_ms})

    # ─── classify ───
    t0 = time.time()
    query_type = await classify_query(query)
    yield _sse("trace", {"node": "classify_query", "result": query_type, "duration_ms": (time.time() - t0) * 1000})
    # Surface any breaker transition the classify-call may have caused (e.g. OPEN→HALF_OPEN
    # because cooldown elapsed and this was the trial request).
    bt = _maybe_breaker_trace()
    if bt:
        yield bt

    # ─── decompose (M5) — only for COMPLEX queries ───
    sub_queries: list[str] = []
    if query_type == "COMPLEX":
        t0 = time.time()
        sub_queries = await decompose_complex(query)
        if sub_queries:
            yield _sse("trace", {
                "node": "decompose",
                "result": f"{len(sub_queries)} sub-queries",
                "detail": " | ".join(sub_queries),
                "duration_ms": (time.time() - t0) * 1000,
            })

    # ─── retrieve + grade loop ───
    retrieve_ms = grade_ms = 0.0
    current_query = query
    retries = 0
    retrieved: list[dict] = []
    graded: list[dict] = []
    grading_result = "IRRELEVANT"

    while True:
        t0 = time.time()
        # M5: when sub-queries exist, retrieve each in parallel and merge via RRF.
        if sub_queries and current_query == query:  # only on first pass; retries use rewrite
            sub_results = await asyncio.gather(*[
                retrieve(os_client, sq, tier, "SIMPLE", settings) for sq in sub_queries
            ], return_exceptions=True)
            seen: dict[str, tuple[float, dict]] = {}
            failed = 0
            for sub_hits in sub_results:
                if isinstance(sub_hits, Exception):
                    failed += 1
                    log.warning("subquery_failed", error=str(sub_hits))
                    continue
                for rank, h in enumerate(sub_hits):
                    cid = h["chunk_id"]
                    score = 1.0 / (60 + rank + 1)
                    prev = seen.get(cid)
                    seen[cid] = ((prev[0] if prev else 0.0) + score, h)
            merged = sorted(seen.values(), key=lambda x: -x[0])[: settings.top_k_rerank]
            retrieved = [m[1] for m in merged]
            if failed:
                yield _sse("trace", {"node": "decompose_partial", "result": f"{failed} sub-query failed", "detail": "merged remaining results", "duration_ms": 0})
        else:
            # Force HyDE on retry passes — gives an extra recall lift on the rewrite query
            # (knob 4: HyDE was SIMPLE-only by default; on a retry we always want it).
            retrieved = await retrieve(os_client, current_query, tier, query_type, settings,
                                       force_hyde=(retries > 0))
        dt = (time.time() - t0) * 1000
        retrieve_ms += dt
        # M4/M5: surface any trace-events the retriever collected (HyDE, rerank, …)
        for sub_evt in getattr(retrieve, "last_trace_events", []) or []:
            yield _sse("trace", sub_evt)
        yield _sse("trace", {"node": "retrieve", "result": f"{len(retrieved)} chunks", "detail": f"tier={tier.value}{' · sub-RRF merged' if sub_queries else ''}", "duration_ms": dt})
        for c in retrieved:
            yield _sse("chunk", {"chunk_id": c["chunk_id"], "hierarchy_path": c.get("hierarchy_path", ""), "status": "retrieved"})

        t0 = time.time()
        grading = await grade_context(query, retrieved, settings)
        dt = (time.time() - t0) * 1000
        grade_ms += dt
        grading_result = grading["overall"]
        graded = grading["relevant_chunks"] or retrieved[:4]
        yield _sse("trace", {"node": "grade_context", "result": grading_result, "detail": f"{len(grading['relevant_chunks'])}/{len(retrieved)} relevant", "duration_ms": dt})
        for c in graded:
            yield _sse("chunk", {"chunk_id": c["chunk_id"], "hierarchy_path": c.get("hierarchy_path", ""), "status": "relevant"})

        if grading_result == "RELEVANT":
            break

        # Knob 2: also retry on IRRELEVANT (was: instant refuse). Many "IRRELEVANT"
        # results are query-formulation issues that survive a rewrite.
        if grading_result in ("AMBIGUOUS", "IRRELEVANT") and retries < settings.max_retries:
            retries += 1
            current_query = await rewrite_query(query)
            yield _sse("trace", {"node": "rewrite_and_retry",
                                 "result": f"retry {retries}/{settings.max_retries} ({grading_result})",
                                 "detail": current_query[:80], "duration_ms": 0})
            continue

        # Knob 3: last-chance AMBIGUOUS-promotion. After exhausted retries, if grading
        # is AMBIGUOUS with ≥1 ambiguous chunk, promote the top-2 to context. The
        # citation-validator stays strict — if the generator can't ground its claims,
        # it still produces an INVALID_CITATIONS refuse. So this is *not* a hallucination
        # weakening, only a "give the model one more chance" affordance.
        if grading_result == "AMBIGUOUS":
            ambiguous_ids = [g["chunk_id"] for g in grading["grades"]
                             if g.get("grade") == "AMBIGUOUS"]
            ambiguous_chunks = [c for c in retrieved if c["chunk_id"] in ambiguous_ids][:2]
            if ambiguous_chunks:
                graded = ambiguous_chunks
                grading_result = "AMBIGUOUS_PROMOTED"
                yield _sse("trace", {
                    "node": "ambiguous_promote",
                    "result": f"{len(ambiguous_chunks)} AMBIGUOUS chunk(s) promoted",
                    "detail": "citation-validator blijft strict; ongegronde antwoorden refusen alsnog",
                    "duration_ms": 0,
                })
                for c in graded:
                    yield _sse("chunk", {"chunk_id": c["chunk_id"],
                                         "hierarchy_path": c.get("hierarchy_path", ""),
                                         "status": "relevant"})
                break

        # ── Diagnose WHY we're refusing — drives the refuse-text and a trace event.
        from app.pipeline.refuse_classifier import classify_refuse
        try:
            diag_emb = await _embedder.embed_query(query)
        except Exception:
            diag_emb = []
        diag = await classify_refuse(os_client, query, diag_emb, tier, settings)
        refuse_category = diag["category"]
        yield _sse("trace", {"node": "refuse_classify", "result": refuse_category,
                             "detail": diag["detail"], "duration_ms": 0})

        refuse_text = _build_refuse_text(diag, tier)
        # M2: emit TTFT for refuse path
        ttft_ms = (time.time() - t_total) * 1000
        yield _sse("ttft", {"ms": round(ttft_ms, 1), "source": "refuse"})
        for piece in _split_for_stream(refuse_text):
            yield _sse("token", piece)
            await asyncio.sleep(0.01)
        yield _sse("trace", {"node": "refuse", "result": grading_result, "duration_ms": 0})
        memory.append_turn(redis_client, session_id, body.query, refuse_text)
        # M10: audit log — refuse path
        audit_log_query(
            redis_client, session_id=session_id, tier=tier.value, query=body.query,
            grade=f"{grading_result}|{refuse_category}", citations=[], ttft_ms=ttft_ms, source="refuse",
        )
        metric_incr(redis_client, f"refuse_{grading_result}")
        metric_incr(redis_client, f"refuse_cat_{refuse_category}")
        yield _sse("done", {"session_id": session_id, "source": "pipeline",
                            "grading_result": grading_result, "refuse_category": refuse_category,
                            "higher_tier_needed": diag.get("higher_tier_needed"),
                            "total_ms": (time.time() - t_total) * 1000})
        return

    # ─── parent expansion (§E in plan) ───
    # For each cited-relevant chunk, if it has a parent_chunk_id that isn't already in graded,
    # fetch the parent and include it as additional context so the LLM sees the surrounding
    # Lid/Artikel — surfacing the hierarchy visibly in the tree.
    graded_ids = {c["chunk_id"] for c in graded}
    parent_chunks: list[dict] = []
    parent_ids_seen: set[str] = set()
    for chunk in graded[:4]:
        pid = chunk.get("parent_chunk_id")
        if not pid or pid in graded_ids or pid in parent_ids_seen:
            continue
        try:
            resp = os_client.get(index=settings.opensearch_index, id=pid, _source_excludes=["embedding"])
            parent = resp["_source"]
            parent_chunks.append(parent)
            parent_ids_seen.add(pid)
            yield _sse("chunk", {
                "chunk_id": parent["chunk_id"],
                "hierarchy_path": parent.get("hierarchy_path", ""),
                "status": "parent_expanded",
                "chunk_text": (parent.get("chunk_text") or "")[:400],
            })
        except Exception:
            continue
    if parent_chunks:
        yield _sse("trace", {
            "node": "parent_expansion",
            "result": f"+{len(parent_chunks)} parent(s)",
            "detail": "hiërarchische context toegevoegd",
            "duration_ms": 0,
        })

    # ─── generate (streaming) ───
    context_parts = []
    for chunk in parent_chunks + graded[:6]:
        context_parts.append(
            f"[chunk_id: {chunk['chunk_id']}]\n"
            f"[path: {chunk.get('hierarchy_path', '')}]\n"
            f"{chunk['chunk_text']}"
        )
    user_prompt = f"Vraag: {query}\n\nContext:\n\n" + "\n\n---\n\n".join(context_parts)

    t0 = time.time()
    full_text = ""
    yield _sse("trace", {"node": "generate", "result": "streaming", "duration_ms": 0})
    async for token in generate_stream(GENERATOR_SYSTEM, user_prompt, temperature=0.0, max_tokens=900):
        # M2: emit TTFT on the very first token from the LLM
        if t_first_token is None:
            t_first_token = time.time()
            ttft_ms = (t_first_token - t_total) * 1000
            yield _sse("ttft", {"ms": round(ttft_ms, 1), "source": "pipeline"})
        full_text += token
        yield _sse("token", token)
    gen_ms = (time.time() - t0) * 1000
    yield _sse("trace", {"node": "generate", "result": "complete", "duration_ms": gen_ms})
    # Recovery transition: HALF_OPEN → CLOSED after successful generate.
    bt = _maybe_breaker_trace()
    if bt:
        yield bt

    if not full_text.strip():
        yield _sse("error", {"detail": "LLM leverde geen content — controleer serverlogs"})
        yield _sse("done", {"session_id": session_id, "source": "pipeline", "grading_result": grading_result, "query_type": query_type, "total_ms": (time.time() - t_total) * 1000})
        return

    # ─── validate ───
    import re
    raw_cited = [cid.strip() for cid in re.findall(r"\[Source:\s*([^\|]+)\s*\|", full_text)]
    known_ids = {c["chunk_id"] for c in graded}
    cited_ids: list[str] = []
    for cid in raw_cited:
        if cid in known_ids:
            cited_ids.append(cid); continue
        # Small-model recovery: ids like "WetIB...::chunk009" often truncate to
        # "chunk009". Accept only when exactly one graded chunk matches.
        if len(cid) >= 4:
            candidates = [k for k in known_ids if cid in k]
            if len(candidates) == 1:
                cited_ids.append(candidates[0])
    cited_ids = list(dict.fromkeys(cited_ids))  # de-dup, preserve order
    if not cited_ids:
        # Last-resort: scan for any known chunk_id literal in the text. If still empty,
        # we will NOT inject `graded[:2]` — that would publish unverified citations.
        cited_ids = [cid for cid in known_ids if cid in full_text]
    validation = validate_citations(full_text, cited_ids, graded)
    if not cited_ids:
        validation = {"valid": False, "reason": "INVALID_CITATIONS — generator produced no verifiable [Source:...] markers"}
    yield _sse("trace", {"node": "validate_output", "result": "PASSED" if validation["valid"] else "FAILED", "detail": validation["reason"], "duration_ms": 1})

    # Fail-CLOSED: if validation failed, refuse with INVALID_CITATIONS reason rather than
    # publish an unverified answer. Replaces the previous fail-OPEN graded[:2] fallback.
    if not validation["valid"]:
        refuse_text = (
            "Ik kon het antwoord niet verifiëren tegen de teruggehaalde bronnen "
            "(citaten ontbraken of pasten niet bij de context). Liever zwijgen dan onjuist "
            "antwoorden. Probeer de vraag specifieker te formuleren of vraag een collega om verificatie."
        )
        yield _sse("text_replace", {"text": refuse_text, "ref_order": []})
        memory.append_turn(redis_client, session_id, body.query, refuse_text)
        metric_incr(redis_client, "refuse_INVALID_CITATIONS")
        audit_log_query(
            redis_client, session_id=session_id, tier=tier.value, query=body.query,
            grade="INVALID_CITATIONS", citations=[],
            ttft_ms=(t_first_token - t_total) * 1000 if t_first_token else None,
            source="refuse",
        )
        yield _sse("done", {
            "session_id": session_id, "source": "pipeline",
            "grading_result": "INVALID_CITATIONS", "query_type": query_type,
            "total_ms": (time.time() - t_total) * 1000,
        })
        return

    # ─── post-process: collapse [Source:…] markers into [N] refs, strip Bronnen footer ───
    # Order the cleaned text in the order chunks were *cited* so [N] aligns with the bubble's
    # citation-pill row that we emit below.
    cleaned_text, ordered_cids = compact_citations(full_text, set(known_ids))
    # If post-processing dropped all markers (shouldn't happen for valid output), fall back to
    # the cited_ids we already validated so the UI still gets a coherent ref order.
    if not ordered_cids:
        ordered_cids = list(cited_ids)
    yield _sse("text_replace", {"text": cleaned_text, "ref_order": ordered_cids})

    citations_out = []
    if validation["valid"]:
        # Emit citations in the order they appear in the cleaned text so the pill row
        # under the bubble matches [1], [2], [3] in the answer.
        graded_by_id = {c["chunk_id"]: c for c in graded}
        for cid in ordered_cids:
            chunk = graded_by_id.get(cid)
            if not chunk:
                continue
            citation = {
                "chunk_id": chunk["chunk_id"],
                "hierarchy_path": chunk.get("hierarchy_path", ""),
                "title": chunk.get("title", ""),
                "article_ref": chunk.get("article_num"),
                "effective_date": chunk.get("effective_date"),
            }
            citations_out.append(citation)
            yield _sse("citation", citation)
            yield _sse("chunk", {"chunk_id": chunk["chunk_id"], "hierarchy_path": chunk.get("hierarchy_path", ""), "status": "cited"})

    # cache the cleaned text so repeat queries serve the formatted version
    if validation["valid"]:
        await store_cache_semantic(
            redis_client,
            query,
            tier,
            cleaned_text,
            citations_out,
            list({c.get("doc_id", "") for c in graded}),
            _embedder,
            query_type=query_type,
        )
        memory.append_turn(redis_client, session_id, body.query, cleaned_text)

    # M10: audit log — pipeline-success path
    audit_log_query(
        redis_client, session_id=session_id, tier=tier.value, query=body.query,
        grade=grading_result,
        citations=[c.get("chunk_id", "") for c in citations_out],
        ttft_ms=(t_first_token - t_total) * 1000 if t_first_token else None,
        source="pipeline",
    )
    yield _sse("done", {
        "session_id": session_id,
        "source": "pipeline",
        "grading_result": grading_result,
        "query_type": query_type,
        "total_ms": (time.time() - t_total) * 1000,
        "retrieve_ms": retrieve_ms,
        "grade_ms": grade_ms,
        "gen_ms": gen_ms,
    })


def _split_for_stream(text: str, chunk_size: int = 24) -> list[str]:
    return [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]


@router.post("/chat", summary="Streaming CRAG chat (SSE)")
async def chat_stream(request: Request, body: ChatRequest):
    async def event_gen():
        try:
            async for evt in _streaming_pipeline(request, body):
                yield evt
        except BreakerOpenError as e:
            req_id = getattr(request.state, "request_id", "n/a")
            log.warning("chat_stream_breaker_open", detail=str(e), request_id=req_id)
            yield _sse("trace", {"node": "refuse", "result": "BREAKER_OPEN", "detail": "circuit_breaker_open", "duration_ms": 0})
            yield _sse("ttft", {"ms": 0.0, "source": "refuse"})
            msg = (
                "Het inferentie-systeem is tijdelijk overbelast en accepteert geen nieuwe "
                "verzoeken. Probeer over enkele minuten opnieuw — de circuit-breaker reset "
                "automatisch zodra de backend weer reageert."
            )
            for piece in _split_for_stream(msg):
                yield _sse("token", piece)
                await asyncio.sleep(0.005)
            yield _sse("done", {"source": "breaker", "total_ms": 0})
        except Exception as e:
            req_id = getattr(request.state, "request_id", "n/a")
            category, friendly = _categorize_error(e)
            log.error("chat_stream_error", error=str(e), error_type=type(e).__name__, request_id=req_id, category=category)
            yield _sse("error", {"category": category, "message": friendly, "request_id": req_id})

    return EventSourceResponse(event_gen())
