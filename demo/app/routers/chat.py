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
from app.pipeline.cache import check_cache_semantic, store_cache_semantic
from app.pipeline.citation_format import compact_citations
from app.pipeline.classifier import classify_query
from app.pipeline.generator import rewrite_query
from app.pipeline.grader import grade_context
from app.pipeline.llm import generate_stream
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
        yield _sse("done", {"session_id": session_id, "source": "cache", "total_ms": (time.time() - t_total) * 1000})
        return
    yield _sse("trace", {"node": "cache_lookup", "result": "MISS", "duration_ms": cache_ms})

    # ─── classify ───
    t0 = time.time()
    query_type = await classify_query(query)
    yield _sse("trace", {"node": "classify_query", "result": query_type, "duration_ms": (time.time() - t0) * 1000})

    # ─── retrieve + grade loop ───
    retrieve_ms = grade_ms = 0.0
    current_query = query
    retries = 0
    retrieved: list[dict] = []
    graded: list[dict] = []
    grading_result = "IRRELEVANT"

    while True:
        t0 = time.time()
        retrieved = await retrieve(os_client, current_query, tier, query_type, settings)
        dt = (time.time() - t0) * 1000
        retrieve_ms += dt
        yield _sse("trace", {"node": "retrieve", "result": f"{len(retrieved)} chunks", "detail": f"tier={tier.value}", "duration_ms": dt})
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
        if grading_result == "AMBIGUOUS" and retries < settings.max_retries:
            retries += 1
            current_query = await rewrite_query(query)
            yield _sse("trace", {"node": "rewrite_and_retry", "result": f"retry {retries}/{settings.max_retries}", "detail": current_query[:80], "duration_ms": 0})
            continue

        # refuse
        refuse_text = (
            "Op basis van de beschikbare documentatie kan ik uw vraag niet beantwoorden. "
            "Probeer uw vraag te herformuleren."
        )
        for piece in _split_for_stream(refuse_text):
            yield _sse("token", piece)
            await asyncio.sleep(0.01)
        yield _sse("trace", {"node": "refuse", "result": grading_result, "duration_ms": 0})
        memory.append_turn(redis_client, session_id, body.query, refuse_text)
        yield _sse("done", {"session_id": session_id, "source": "pipeline", "grading_result": grading_result, "total_ms": (time.time() - t_total) * 1000})
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
        full_text += token
        yield _sse("token", token)
    gen_ms = (time.time() - t0) * 1000
    yield _sse("trace", {"node": "generate", "result": "complete", "duration_ms": gen_ms})

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
        cited_ids = [cid for cid in known_ids if cid in full_text]
    if not cited_ids and graded:
        cited_ids = [c["chunk_id"] for c in graded[:2]]
    validation = validate_citations(full_text, cited_ids, graded)
    yield _sse("trace", {"node": "validate_output", "result": "PASSED" if validation["valid"] else "FAILED", "detail": validation["reason"], "duration_ms": 1})

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
        except Exception as e:
            log.error("chat_stream_error", error=str(e))
            yield {"event": "error", "data": json.dumps({"detail": str(e)})}

    return EventSourceResponse(event_gen())
