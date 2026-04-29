"""Simple circuit-breaker for the LLM client.

States: CLOSED → (n failures within window) → OPEN → (cooldown) → HALF_OPEN
        → (1 success) → CLOSED
        → (1 failure)  → OPEN

Defaults are tuned for a CPU-only laptop demo: 3 failures in 30s opens the
breaker, then we wait 20s before letting one trial request through.

The breaker is in-process and process-wide. With a single uvicorn worker
that's exactly what we want; if the demo ever scales to multi-worker we'd
swap this for a Redis-backed token-bucket.
"""
from __future__ import annotations

import time
from enum import Enum
import structlog

log = structlog.get_logger()


class State(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class BreakerOpenError(RuntimeError):
    """Raised by `before()` when the breaker is OPEN and cooldown has not elapsed."""


class CircuitBreaker:
    def __init__(self, threshold: int = 3, window_s: float = 30.0, recover_after_s: float = 20.0):
        self.threshold = threshold
        self.window_s = window_s
        self.recover_after_s = recover_after_s
        self.state: State = State.CLOSED
        self._failures: list[float] = []
        self._opened_at: float | None = None
        # Last state-transition recorded as (old, new, ts). Routers can read this
        # right after a request to decide whether to emit a trace event to the UI.
        self.last_transition: tuple[State, State, float] | None = None

    def _transition(self, new_state: State) -> None:
        if new_state != self.state:
            self.last_transition = (self.state, new_state, time.time())
            self.state = new_state

    # ── public API ──
    def before(self) -> None:
        """Call before issuing an LLM request; raises BreakerOpenError when open."""
        if self.state == State.OPEN:
            if self._opened_at is not None and (time.time() - self._opened_at) >= self.recover_after_s:
                log.info("breaker_half_open")
                self._transition(State.HALF_OPEN)
            else:
                raise BreakerOpenError("LLM-service tijdelijk onbeschikbaar (circuit open).")

    def on_success(self) -> None:
        if self.state == State.HALF_OPEN:
            log.info("breaker_closed_after_recovery")
            self._transition(State.CLOSED)
            self._failures.clear()

    def on_failure(self) -> None:
        now = time.time()
        self._failures.append(now)
        self._failures = [t for t in self._failures if now - t < self.window_s]
        if self.state == State.HALF_OPEN or len(self._failures) >= self.threshold:
            log.warning("breaker_opened", failures=len(self._failures))
            self._transition(State.OPEN)
            self._opened_at = now

    def consume_transition(self) -> tuple[State, State, float] | None:
        """Pop the last transition (router emits it as a trace event, then clears)."""
        t = self.last_transition
        self.last_transition = None
        return t

    def status(self) -> dict:
        return {
            "state": self.state.value,
            "recent_failures": len(self._failures),
            "threshold": self.threshold,
            "window_s": self.window_s,
            "opened_at": self._opened_at,
            "cooldown_remaining_s": max(0.0, self.recover_after_s - (time.time() - (self._opened_at or 0))) if self.state == State.OPEN else 0.0,
        }


# Process-wide singleton.
breaker = CircuitBreaker()
