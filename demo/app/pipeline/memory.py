"""
Redis-backed conversation memory.

Stores the last N turns per session_id so the classifier and query rewriter
can resolve anaphora like "en als het kind ouder is dan 6?".
"""

from __future__ import annotations

import json
import time
from typing import Optional

from redis import Redis

from app.config import get_settings


def _key(session_id: str) -> str:
    return f"conv:{session_id}"


def append_turn(
    redis_client: Redis,
    session_id: str,
    user_query: str,
    assistant_response: str,
) -> None:
    settings = get_settings()
    key = _key(session_id)
    entry = json.dumps(
        {"q": user_query, "a": assistant_response[:800], "t": int(time.time())}
    )
    pipe = redis_client.pipeline()
    pipe.rpush(key, entry)
    pipe.ltrim(key, -settings.max_conversation_turns, -1)
    pipe.expire(key, 3600)
    pipe.execute()


def load_history(redis_client: Redis, session_id: str) -> list[dict]:
    raw = redis_client.lrange(_key(session_id), 0, -1)
    return [json.loads(x) for x in raw]


def format_history_for_prompt(history: list[dict], max_chars: int = 1200) -> str:
    if not history:
        return ""
    lines: list[str] = []
    for turn in history:
        lines.append(f"User: {turn['q']}")
        lines.append(f"Assistant: {turn['a']}")
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[-max_chars:]
    return text


async def resolve_followup(
    redis_client: Redis,
    session_id: str,
    query: str,
) -> tuple[str, Optional[str]]:
    """
    If the query looks like a follow-up (short, contains anaphora markers),
    rewrite it standalone using prior turns. Returns (resolved_query, original_if_rewritten).
    """
    history = load_history(redis_client, session_id)
    if not history:
        return query, None

    markers = ["en als", "en wat", "en hoe", "en bij", "daarvoor", "daarvan", "en dan", "ook voor", "dezelfde"]
    lowered = query.lower()
    looks_like_followup = len(query.split()) < 12 and any(m in lowered for m in markers)
    if not looks_like_followup:
        return query, None

    from app.pipeline.llm import generate

    system = (
        "Je bent een herschrijver voor Nederlandse belastingvragen. "
        "Gegeven een gespreksgeschiedenis en een vervolgvraag, herschrijf de vervolgvraag "
        "tot een op zichzelf staande vraag die zonder context begrepen kan worden. "
        "Geef ALLEEN de herschreven vraag terug, geen uitleg."
    )
    user = f"Geschiedenis:\n{format_history_for_prompt(history)}\n\nVervolgvraag: {query}\n\nHerschreven vraag:"
    rewritten = await generate(system, user, temperature=0.0, max_tokens=120)
    rewritten = rewritten.strip().strip('"').strip("'")
    if rewritten and rewritten != query:
        return rewritten, query
    return query, None
