"""
SSE-driven ingestion endpoint.

POST /v1/ingest accepts a PDF/TXT upload (multipart/form-data) and streams per-chunk
events as each chunk moves through chunking → enrichment → embedding → indexing.
"""

from __future__ import annotations

import io
import json
import re
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Form, Request, UploadFile, File, HTTPException
from sse_starlette.sse import EventSourceResponse
import structlog

from app.config import get_settings
from app.ingestion.pipeline import DocInput, ingest_document
from app.models import SecurityTier

log = structlog.get_logger()
router = APIRouter()


def _safe_doc_id(title: str) -> str:
    base = re.sub(r"[^A-Za-z0-9_-]+", "-", title).strip("-")
    return base[:60] or "doc"


def _extract_text_from_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise RuntimeError("pypdf not installed — add to requirements-demo.txt")
    reader = PdfReader(io.BytesIO(data))
    pages = []
    for p in reader.pages:
        try:
            pages.append(p.extract_text() or "")
        except Exception as e:
            log.warning("pdf_page_extract_failed", error=str(e))
    return "\n\n".join(pages)


_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB cap — prevents accidental huge-file ingest


@router.post("/ingest", summary="Upload a document and stream per-chunk ingestion (SSE)")
async def ingest(
    request: Request,
    file: UploadFile = File(...),
    title: str | None = Form(None, max_length=200),
    security_classification: str = Form("PUBLIC", max_length=32),
):
    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail="Empty file upload")
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File too large (>{_MAX_UPLOAD_BYTES // 1024 // 1024} MB)")
    raw_title = (title or Path(file.filename or "document").stem).strip()
    if not raw_title:
        raise HTTPException(status_code=422, detail="Title cannot be empty")
    doc_id = _safe_doc_id(raw_title) + f"-v{date.today().year}"

    # text extraction
    if file.filename and file.filename.lower().endswith(".pdf"):
        try:
            text = _extract_text_from_pdf(data)
        except Exception as e:
            log.error("pdf_extract_failed", error=str(e))
            return {"error": f"PDF-extractie mislukt: {e}"}
    else:
        text = data.decode("utf-8", errors="replace")

    # S3.3 — PDF leegheid-check: scan-only PDFs yield ~0 extractable text.
    # Block early rather than ingest a single empty chunk that pretends to succeed.
    if len(text.strip()) < 100:
        async def _empty_stream():
            yield {"event": "error", "data": json.dumps({
                "error": "no_text_extracted",
                "hint": "Deze PDF bevat geen extraheerbare tekst (waarschijnlijk een scan). Probeer een tekst-PDF of gebruik OCR.",
                "extracted_chars": len(text.strip()),
            })}
        return EventSourceResponse(_empty_stream())

    try:
        tier = SecurityTier(security_classification)
    except ValueError:
        tier = SecurityTier.PUBLIC

    doc = DocInput(
        doc_id=doc_id,
        title=raw_title,
        text=text,
        security_classification=tier.value,
        effective_date=date.today().isoformat(),
    )

    async def event_stream():
        try:
            async for evt in ingest_document(doc, request.app.state.opensearch):
                kind = evt.get("type", "trace")
                yield {"event": kind, "data": json.dumps(evt, default=str)}
        except Exception as e:
            log.error("ingest_stream_error", error=str(e))
            yield {"event": "error", "data": json.dumps({"detail": str(e)})}

    return EventSourceResponse(event_stream())


@router.get("/documents/{doc_id}/chunks", summary="List chunks of a single document")
async def list_doc_chunks(request: Request, doc_id: str):
    settings = get_settings()
    os_client = request.app.state.opensearch
    body = {
        "query": {"term": {"doc_id": doc_id}},
        "size": 500,
        "sort": [{"chunk_sequence": "asc"}],
        "_source": {"excludes": ["embedding"]},
    }
    resp = os_client.search(index=settings.opensearch_index, body=body)
    chunks = [h["_source"] for h in resp["hits"]["hits"]]
    return {"doc_id": doc_id, "chunks": chunks}


@router.get("/chunks/{chunk_id}", summary="Fetch one chunk by chunk_id")
async def get_chunk(request: Request, chunk_id: str):
    settings = get_settings()
    os_client = request.app.state.opensearch
    try:
        resp = os_client.get(
            index=settings.opensearch_index,
            id=chunk_id,
            _source_excludes=["embedding"],
        )
        return resp["_source"]
    except Exception:
        return {"error": "not_found", "chunk_id": chunk_id}


@router.get("/documents", summary="List ingested documents")
async def list_documents(request: Request):
    settings = get_settings()
    os_client = request.app.state.opensearch
    body = {
        "size": 0,
        "aggs": {
            "by_doc": {
                "terms": {"field": "doc_id", "size": 50},
                "aggs": {
                    "title":   {"top_hits": {"size": 1, "_source": {"includes": ["title", "doc_type", "security_classification", "effective_date", "hierarchy_path"]}}},
                }
            }
        }
    }
    resp = os_client.search(index=settings.opensearch_index, body=body)
    buckets = resp.get("aggregations", {}).get("by_doc", {}).get("buckets", [])
    documents = []
    for b in buckets:
        hit = b["title"]["hits"]["hits"][0]["_source"] if b["title"]["hits"]["hits"] else {}
        documents.append({
            "doc_id": b["key"],
            "chunk_count": b["doc_count"],
            "title": hit.get("title", b["key"]),
            "doc_type": hit.get("doc_type"),
            "security_classification": hit.get("security_classification"),
            "effective_date": hit.get("effective_date"),
        })
    return {"documents": documents}
