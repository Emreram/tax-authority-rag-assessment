"""Demo-only chaos endpoints — for showing fail-safe behaviour live.

Production deploy must disable this router (or gate behind admin auth).
"""
from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Request

from app.pipeline.breaker import breaker, State

router = APIRouter()


@router.post("/admin/chaos/force_breaker_open", summary="Trip the LLM circuit-breaker")
async def force_breaker_open():
    breaker.state = State.OPEN
    breaker._opened_at = time.time()
    return {"ok": True, "state": breaker.state.value, "cooldown_s": breaker.recover_after_s}


@router.post("/admin/chaos/reset_breaker", summary="Force-close the breaker")
async def reset_breaker():
    breaker.state = State.CLOSED
    breaker._failures.clear()
    breaker._opened_at = None
    return {"ok": True, "state": breaker.state.value}


@router.get("/admin/chaos/breaker_status", summary="Inspect the breaker state")
async def breaker_status():
    return breaker.status()
