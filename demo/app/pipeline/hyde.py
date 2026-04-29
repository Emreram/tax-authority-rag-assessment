"""
HyDE — Hypothetical Document Embeddings.

For SIMPLE queries the LLM generates a plausible (but unverified) answer, we
embed that, and use the embedding for kNN alongside the raw query. This lifts
recall when the query is terse and far from the document vocabulary (e.g.
"arbeidskorting" → drafted answer mentions 'korting op loonbelasting voor
werkenden' which is closer to the actual text).

If the drafted answer is empty or fails, we fall back to the raw query.
"""

from __future__ import annotations

import structlog

from app.pipeline.llm import generate

log = structlog.get_logger()

HYDE_SYSTEM = """Je bent een belastingexpert. Gegeven een vraag, schrijf een korte
hypothetische passage (max 2 zinnen, max 40 woorden) die antwoord zou geven. Gebruik
Nederlandse fiscale terminologie. Geen disclaimers, geen vragen terug.
"""


async def draft_hypothesis(query: str) -> str:
    from app.config import get_settings
    try:
        raw = await generate(HYDE_SYSTEM, query, temperature=0.2, max_tokens=80,
                             timeout=get_settings().llm_timeout_hyde_s)
    except Exception as e:
        log.warning("hyde_draft_failed", error=str(e))
        return ""
    return raw.strip()[:400]
