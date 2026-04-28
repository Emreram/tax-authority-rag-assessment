#!/usr/bin/env python3
"""
Fires a set of demo queries through the pipeline immediately after startup.

Why: semantic cache turns cold queries into hot ones; the first live query then
returns in <200ms. The audience never waits on a cold LLM.

Usage (inside API container):
    python /app/scripts/prewarm_cache.py
Or from host with the stack running:
    python demo/scripts/prewarm_cache.py --host http://localhost:8000
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request

DEMO_QUERIES = [
    ("Wat is de arbeidskorting in 2024?", "PUBLIC"),
    ("Wat zijn de BTW-tarieven in Nederland?", "PUBLIC"),
    ("ECLI:NL:HR:2021:1523", "PUBLIC"),
    ("Hoe werkt de hypotheekrenteaftrek?", "PUBLIC"),
    ("Wat zijn de termijnen voor bezwaar?", "INTERNAL"),
    ("Hoe werkt de Handboek Invordering procedure?", "INTERNAL"),
    ("Wat is transfer pricing onderzoek methodologie?", "RESTRICTED"),
    ("Wat zijn de FIOD opsporingsmethoden voor BTW-fraude?", "CLASSIFIED_FIOD"),
]


def fire(host: str, query: str, tier: str) -> dict:
    body = json.dumps({
        "query": query, "security_tier": tier, "session_id": "prewarm"
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{host.rstrip('/')}/v1/query",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=120) as resp:
        payload = json.loads(resp.read())
    return {"query": query, "tier": tier, "source": payload.get("source"), "ms": (time.time() - t0) * 1000}


def wait_for_ready(host: str, max_seconds: int = 240) -> bool:
    deadline = time.time() + max_seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{host.rstrip('/')}/health", timeout=5) as r:
                if json.loads(r.read()).get("warmup_complete"):
                    return True
        except Exception:
            pass
        time.sleep(2)
    return False


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="http://localhost:8000")
    args = p.parse_args()

    print(f"[prewarm] waiting for {args.host}/health ...")
    if not wait_for_ready(args.host):
        print("[prewarm] API didn't become ready in time — aborting")
        return 1

    # Fire twice: first pass populates cache, second pass verifies <200ms.
    for label in ("cold", "warm"):
        print(f"[prewarm] pass: {label}")
        for q, tier in DEMO_QUERIES:
            try:
                r = fire(args.host, q, tier)
                print(f"  {tier:16s} {r['source']:8s} {r['ms']:6.0f}ms  {q[:60]}")
            except Exception as e:
                print(f"  {tier:16s} ERROR              {q[:60]}  ({e})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
