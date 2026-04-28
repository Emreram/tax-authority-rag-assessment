"""
Semantic chunker — LLM-driven boundary proposal for documents without structural markers.

Ollama is asked to return a JSON array of cut points with a one-line reason for each.
Results are cached against sha256(doc_text) so re-ingestion is deterministic.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import structlog

from app.pipeline.llm import generate_json

log = structlog.get_logger()


SYSTEM_PROMPT = """Je bent een documentanalyzer voor de Nederlandse Belastingdienst.
Je taak: stel semantische breukpunten voor in het document zodat elk segment één coherente gedachte bevat.

Regels:
- Doelgrootte per segment: 400–900 tekens.
- Cut alleen bij duidelijke thema- of onderwerpwisselingen.
- Elke cut heeft een korte Nederlandse reden (max 12 woorden).
- Het eerste segment begint op offset 0 (impliciet, niet in lijst).

Antwoord in dit JSON-formaat:
{"cuts": [{"offset": <int>, "reason": "<string>"}, ...]}
"""


@dataclass
class SemanticCut:
    offset: int
    reason: str


_cache: dict[str, list[SemanticCut]] = {}


def _cache_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def propose_cuts(text: str, max_cuts: int = 12) -> list[SemanticCut]:
    key = _cache_key(text)
    if key in _cache:
        return _cache[key]

    preview = text if len(text) <= 6000 else text[:3000] + "\n...\n" + text[-2500:]
    user_prompt = f"Document ({len(text)} tekens):\n---\n{preview}\n---\n\nGeef maximaal {max_cuts} cuts."

    try:
        raw = await generate_json(SYSTEM_PROMPT, user_prompt, temperature=0.0, max_tokens=512)
        cuts_raw = raw.get("cuts", []) if isinstance(raw, dict) else []
    except Exception as e:
        log.warning("semantic_chunker_failed", error=str(e))
        cuts_raw = []

    cuts: list[SemanticCut] = []
    for c in cuts_raw:
        try:
            offset = int(c["offset"])
            reason = str(c.get("reason", "")).strip()
            if 0 < offset < len(text):
                cuts.append(SemanticCut(offset=offset, reason=reason or "onderwerpwisseling"))
        except (KeyError, ValueError, TypeError):
            continue

    # Deduplicate + sort
    cuts = sorted({c.offset: c for c in cuts}.values(), key=lambda c: c.offset)
    _cache[key] = cuts
    log.info("semantic_cuts_proposed", count=len(cuts), doc_hash=key[:8])
    return cuts


def cuts_to_segments(text: str, cuts: list[SemanticCut]) -> list[tuple[int, int, str]]:
    """Turn ordered cuts into (start, end, text) tuples covering the whole doc."""
    offsets = [0] + [c.offset for c in cuts] + [len(text)]
    segs: list[tuple[int, int, str]] = []
    for i in range(len(offsets) - 1):
        s, e = offsets[i], offsets[i + 1]
        if e - s < 40:
            continue
        segs.append((s, e, text[s:e].strip()))
    return segs
