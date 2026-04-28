"""
Ingestion dispatcher — glues together parsing, chunking, metadata inheritance,
LLM enrichment, embedding, and OpenSearch indexing.

Emits per-chunk progress events so the UI can render a live stream.
"""

from __future__ import annotations

import io
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import AsyncIterator

import structlog
from opensearchpy import OpenSearch

from app.config import get_settings
from app.ingestion import semantic_chunker, structural_chunker
from app.ingestion.chunk_id import build_chunk_id, build_hierarchy_path
from app.ingestion.metadata_enricher import enrich
from app.ingestion.structural_chunker import Boundary
from app.pipeline.embedder import embed_document

log = structlog.get_logger()


@dataclass
class DocInput:
    doc_id: str
    title: str
    text: str
    doc_type: str | None = None
    security_classification: str = "PUBLIC"
    effective_date: str = "2024-01-01"
    version: int = 1
    language: str = "nl"
    source_url: str | None = None
    ecli_id: str | None = None


def _token_count(text: str) -> int:
    # Cheap approximation — we don't have the e5 tokenizer loaded in this path.
    return max(1, len(text.split()))


def _fallback_one_boundary(doc: DocInput) -> list[Boundary]:
    return [Boundary("document", "1", "Document", 0, len(doc.text), doc.text)]


def _boundary_to_hierarchy(b: Boundary, doc_type: str) -> dict:
    h: dict[str, str | None] = {
        "chapter": None, "section": None,
        "article_num": None, "paragraph_num": None, "sub_paragraph": None,
    }
    if b.level == "chapter":
        h["chapter"] = b.identifier
    elif b.level == "section":
        h["section"] = b.identifier
    elif b.level == "article":
        h["article_num"] = b.identifier
    elif b.level == "paragraph":
        h["paragraph_num"] = b.identifier
    elif b.level == "sub_paragraph":
        h["sub_paragraph"] = b.identifier
    elif b.level == "consideration":
        h["article_num"] = b.identifier
    return h


async def ingest_document(
    doc: DocInput,
    os_client: OpenSearch,
) -> AsyncIterator[dict]:
    """
    Emits events:
      - parsed           {chars}
      - chunker_choice   {path: structural|semantic|single, doc_type, reason}
      - semantic_cut     {offset, reason}             (only for semantic path)
      - chunk_started    {chunk, total, chunk_id, hierarchy_path, text_preview, level}
      - chunk_enriched   {chunk_id, topic, entities, summary}
      - chunk_embedded   {chunk_id, dim}
      - chunk_indexed    {chunk_id}
      - complete         {chunks, total_ms}
      - error            {detail}
    """
    settings = get_settings()
    t0 = time.time()
    yield {"type": "parsed", "chars": len(doc.text)}

    # ─── pick chunking path ───
    has_structural = structural_chunker.has_structural_markers(doc.text)
    if has_structural:
        boundaries, detected_type = structural_chunker.detect(doc.text, doc.doc_type)
        path = "structural"
        yield {
            "type": "chunker_choice",
            "path": path,
            "doc_type": detected_type,
            "reason": "structurele markers gevonden (Hoofdstuk/Artikel/Lid)",
            "boundaries": len(boundaries),
        }
    else:
        yield {
            "type": "chunker_choice",
            "path": "semantic",
            "doc_type": doc.doc_type or "UNSTRUCTURED",
            "reason": "geen structurele markers — AI bepaalt semantische grenzen",
            "boundaries": 0,
        }
        cuts = await semantic_chunker.propose_cuts(doc.text)
        for c in cuts:
            yield {"type": "semantic_cut", "offset": c.offset, "reason": c.reason}
        segs = semantic_chunker.cuts_to_segments(doc.text, cuts)
        boundaries = [
            Boundary("section", str(i + 1), f"Segment {i + 1}", s, e, body)
            for i, (s, e, body) in enumerate(segs)
        ]
        detected_type = doc.doc_type or "UNSTRUCTURED"
        path = "semantic"

    if not boundaries:
        boundaries = _fallback_one_boundary(doc)
        path = "single"
        yield {"type": "chunker_choice", "path": "single", "doc_type": detected_type,
               "reason": "geen bruikbare grenzen — document als één chunk", "boundaries": 1}

    total = len(boundaries)

    # ─── chunk + enrich + embed + index loop ───
    for seq, b in enumerate(boundaries):
        h = _boundary_to_hierarchy(b, detected_type)
        chunk_id = build_chunk_id(
            doc_id=doc.doc_id,
            chapter=h["chapter"],
            section=h["section"],
            article_num=h["article_num"],
            paragraph_num=h["paragraph_num"],
            sub_paragraph=h["sub_paragraph"],
            chunk_sequence=seq,
        )
        # parent = same hierarchy but one level up (first non-null parent ancestor)
        parent_h = dict(h)
        # Drop the deepest known level to get parent
        for key in ("sub_paragraph", "paragraph_num", "article_num", "section", "chapter"):
            if parent_h.get(key):
                parent_h[key] = None
                break
        parent_chunk_id = None
        if any(parent_h.values()):
            parent_chunk_id = build_chunk_id(
                doc_id=doc.doc_id, **parent_h, chunk_sequence=0
            )

        hierarchy_path = build_hierarchy_path(
            doc_title=doc.title,
            chapter=h["chapter"], section=h["section"],
            article_num=h["article_num"], paragraph_num=h["paragraph_num"],
            sub_paragraph=h["sub_paragraph"],
            consideration=h["article_num"] if detected_type == "CASE_LAW" else None,
        )

        text_preview = b.text[:160]
        yield {
            "type": "chunk_started",
            "chunk": seq + 1, "total": total,
            "chunk_id": chunk_id,
            "parent_chunk_id": parent_chunk_id,
            "hierarchy_path": hierarchy_path,
            "level": b.level,
            "text_preview": text_preview,
        }

        # enrichment (LLM)
        enrichment = await enrich(b.text, hierarchy_path)
        yield {"type": "chunk_enriched", "chunk_id": chunk_id, **enrichment}

        # embedding
        vector = await embed_document(b.text)
        yield {"type": "chunk_embedded", "chunk_id": chunk_id, "dim": len(vector)}

        # indexing
        document = {
            "chunk_id": chunk_id,
            "doc_id": doc.doc_id,
            "doc_type": detected_type,
            "title": doc.title,
            "article_num": h["article_num"],
            "paragraph_num": h["paragraph_num"],
            "chapter": h["chapter"],
            "hierarchy_path": hierarchy_path,
            "effective_date": doc.effective_date,
            "expiry_date": None,
            "version": doc.version,
            "security_classification": doc.security_classification,
            "language": doc.language,
            "ecli_id": doc.ecli_id,
            "chunk_sequence": seq,
            "token_count": _token_count(b.text),
            "ingestion_timestamp": datetime.utcnow().isoformat() + "Z",
            "source_url": doc.source_url,
            "parent_chunk_id": parent_chunk_id,
            "chunk_text": b.text,
            "topic": enrichment.get("topic") or None,
            "entities": enrichment.get("entities") or [],
            "summary": enrichment.get("summary") or None,
            "embedding": vector,
        }
        os_client.index(
            index=settings.opensearch_index,
            id=chunk_id,
            body=document,
            refresh=False,
        )
        yield {"type": "chunk_indexed", "chunk_id": chunk_id}

    # refresh once at the end so the new chunks are searchable immediately
    os_client.indices.refresh(index=settings.opensearch_index)

    yield {"type": "complete", "chunks": total, "total_ms": (time.time() - t0) * 1000}
