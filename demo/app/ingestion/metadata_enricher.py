"""
Metadata enricher — uses Ollama (JSON mode) to add semantic metadata to each chunk.

Inputs per chunk: raw text + already-known hierarchical fields.
Outputs: topic, entities, amendment_refs, summary.

Falls back silently if the LLM returns unparseable JSON — the hierarchical fields
built by structural/semantic chunker + inheritance manager are always populated
deterministically, so enrichment is additive, never on the critical path.
"""

from __future__ import annotations

import structlog

from app.pipeline.llm import generate_json

log = structlog.get_logger()


SYSTEM_PROMPT = """Je analyseert een fragment uit een Nederlands belastingdocument.
Geef terug in JSON:
- topic: hoofdonderwerp in 2–4 Nederlandse woorden
- entities: array met genoemde wetsartikelen, bedragen, data (max 5)
- summary: één Nederlandse zin die de inhoud samenvat (max 20 woorden)

Voorbeeld:
{"topic": "arbeidskorting 2024", "entities": ["art. 3.114", "€ 5.532"], "summary": "De arbeidskorting 2024 bedraagt maximaal € 5.532."}
"""


async def enrich(chunk_text: str, hierarchy_path: str) -> dict:
    preview = chunk_text if len(chunk_text) <= 1500 else chunk_text[:1500]
    user = f"Hiërarchie: {hierarchy_path}\n\nFragment:\n{preview}"
    try:
        raw = await generate_json(SYSTEM_PROMPT, user, temperature=0.0, max_tokens=220)
    except Exception as e:
        log.warning("enrichment_failed", error=str(e))
        raw = {}

    topic = str(raw.get("topic", "")).strip()[:60] if isinstance(raw, dict) else ""
    entities = raw.get("entities", []) if isinstance(raw, dict) else []
    if not isinstance(entities, list):
        entities = []
    entities = [str(e).strip()[:60] for e in entities[:5] if str(e).strip()]
    summary = str(raw.get("summary", "")).strip()[:300] if isinstance(raw, dict) else ""

    return {"topic": topic, "entities": entities, "summary": summary}
