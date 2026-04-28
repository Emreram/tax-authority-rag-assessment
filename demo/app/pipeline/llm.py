"""
LLM wrapper — talks to Docker Model Runner via OpenAI-compatible API.

Exposes:
  - generate(system, user, temperature, max_tokens) -> str
  - generate_stream(system, user, ...) -> async iterator of token strings
  - generate_json(system, user) -> dict
  - ping() -> bool
"""

from __future__ import annotations

import json
from typing import AsyncIterator, Optional

import httpx
from openai import AsyncOpenAI
import structlog

from app.config import get_settings
from app.pipeline.breaker import breaker, BreakerOpenError  # noqa: F401  (re-exported for routers)

log = structlog.get_logger()

_client: AsyncOpenAI | None = None


def get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        s = get_settings()
        _client = AsyncOpenAI(
            base_url=s.llm_base_url,
            api_key="not-needed",
            timeout=s.llm_timeout_s,
        )
    return _client


async def generate(
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.0,
    max_tokens: int = 512,
    model: Optional[str] = None,
) -> str:
    s = get_settings()
    breaker.before()
    try:
        resp = await get_client().chat.completions.create(
            model=model or s.llm_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            stream=False,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        breaker.on_success()
        return resp.choices[0].message.content or ""
    except BreakerOpenError:
        raise
    except Exception:
        breaker.on_failure()
        raise


async def generate_stream(
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.2,
    max_tokens: int = 512,
    model: Optional[str] = None,
) -> AsyncIterator[str]:
    s = get_settings()
    breaker.before()
    got_any = False
    try:
        stream = await get_client().chat.completions.create(
            model=model or s.llm_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content or ""
            if delta:
                got_any = True
                yield delta
        if got_any:
            breaker.on_success()
        else:
            breaker.on_failure()
    except BreakerOpenError:
        raise
    except Exception:
        breaker.on_failure()
        raise


async def generate_json(
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.0,
    max_tokens: int = 1024,
    model: Optional[str] = None,
) -> dict:
    """Structured JSON output. Falls back to prompt-parsing if response_format unsupported."""
    s = get_settings()
    breaker.before()
    try:
        resp = await get_client().chat.completions.create(
            model=model or s.llm_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        text = resp.choices[0].message.content or ""
        breaker.on_success()
    except BreakerOpenError:
        raise
    except Exception as e:
        log.warning("json_mode_unsupported_fallback_to_plain", error=str(e))
        # Note: generate() has its own breaker tracking, so we don't double-count here.
        text = await generate(
            system_prompt + "\n\nRespond ONLY with a valid JSON object, nothing else.",
            user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            model=model,
        )
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        import re
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            return json.loads(m.group(0))
        log.warning("json_parse_failed", raw=text[:200])
        raise


async def ping() -> bool:
    s = get_settings()
    try:
        async with httpx.AsyncClient(timeout=5) as http:
            r = await http.get(f"{s.llm_base_url}/models")
            return r.status_code == 200
    except Exception:
        return False
