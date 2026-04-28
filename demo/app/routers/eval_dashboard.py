"""
Golden-set eval dashboard.

GET /eval runs each entry in golden_test_set_spec.json through the pipeline and
renders pass/fail as an HTML table. Enough for a live-demo credibility beat.
Not intended as a full Ragas replacement — this is smoke-level validation.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
import structlog

from app.models import SecurityTier
from app.pipeline.crag import run_crag

log = structlog.get_logger()
router = APIRouter()

GOLDEN_PATH_CANDIDATES = [
    Path("/app/eval/golden_test_set_spec.json"),
    Path(__file__).parents[3] / "eval" / "golden_test_set_spec.json",
]


def _load_golden() -> list[dict]:
    for p in GOLDEN_PATH_CANDIDATES:
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            return data.get("entries", data if isinstance(data, list) else [])
    return []


def _check_entry(entry: dict, result) -> tuple[bool, list[str]]:
    """Returns (passed, failure_reasons)."""
    reasons: list[str] = []
    text = (result.response or "").lower()

    if entry.get("must_refuse"):
        if result.grading_result == "IRRELEVANT" or "niet beantwoorden" in text:
            return True, []
        reasons.append("expected refuse, got answer")
        return False, reasons

    for needed in entry.get("expected_answer_contains", []):
        if needed.lower() not in text:
            reasons.append(f"missing phrase: {needed!r}")

    expected_ids = set(entry.get("expected_chunk_ids", []))
    if expected_ids:
        cited = {c.chunk_id for c in (result.citations or [])}
        if not (expected_ids & cited):
            reasons.append(f"no expected chunk cited; got {sorted(cited)}")

    return (len(reasons) == 0), reasons


@router.get("/eval", response_class=HTMLResponse)
async def eval_dashboard(request: Request):
    entries = _load_golden()
    if not entries:
        return HTMLResponse("<h2>No golden set found.</h2>", status_code=404)

    os_client = request.app.state.opensearch
    redis_client = request.app.state.redis

    rows: list[dict] = []
    for entry in entries:
        t0 = time.time()
        try:
            result = await run_crag(
                query=entry["query"],
                security_tier=SecurityTier(entry.get("security_tier", "PUBLIC")),
                session_id="eval",
                os_client=os_client,
                redis_client=redis_client,
            )
            passed, reasons = _check_entry(entry, result)
        except Exception as e:
            result = None
            passed = False
            reasons = [f"exception: {e}"]
        rows.append({
            "id": entry.get("id"),
            "query": entry["query"],
            "tier": entry.get("security_tier", "PUBLIC"),
            "passed": passed,
            "reasons": reasons,
            "ms": (time.time() - t0) * 1000,
            "source": result.source if result else "error",
            "grading_result": result.grading_result if result else None,
        })

    passed_count = sum(1 for r in rows if r["passed"])
    total = len(rows)
    pct = (passed_count / total * 100) if total else 0

    def row_html(r):
        bg = "#e8f5e9" if r["passed"] else "#ffebee"
        color = "#2e7d32" if r["passed"] else "#c62828"
        label = "PASS" if r["passed"] else "FAIL"
        reasons = ("<br>".join(r["reasons"])) if r["reasons"] else ""
        return f"""
          <tr style="background:{bg}">
            <td><code>{r['id']}</code></td>
            <td>{r['query']}</td>
            <td>{r['tier']}</td>
            <td><strong style="color:{color}">{label}</strong></td>
            <td>{r['grading_result'] or ''}</td>
            <td>{r['source']}</td>
            <td>{r['ms']:.0f}</td>
            <td style="color:#c62828;font-size:12px">{reasons}</td>
          </tr>"""

    html = f"""<!doctype html>
<html lang="nl"><head><meta charset="utf-8"><title>Golden-set evaluatie</title>
<style>
  body {{ font-family: 'Segoe UI', sans-serif; padding: 24px; color: #15202b; background: #f3f5f7; }}
  h1 {{ color: #01689B; }}
  .summary {{ background: white; padding: 16px; border-radius: 8px; margin-bottom: 16px; box-shadow: 0 1px 2px rgba(0,0,0,.05); }}
  table {{ width: 100%; border-collapse: collapse; background: white; border-radius: 8px; overflow: hidden; }}
  th {{ background: #01689B; color: white; text-align: left; padding: 10px; }}
  td {{ padding: 8px 10px; border-bottom: 1px solid #e1e5ea; font-size: 14px; }}
  code {{ font-size: 12px; }}
</style>
</head><body>
<h1>Golden-set evaluatie</h1>
<div class="summary">
  <strong>{passed_count} / {total} geslaagd ({pct:.0f}%)</strong>
</div>
<table>
  <thead><tr>
    <th>ID</th><th>Query</th><th>Tier</th><th>Resultaat</th>
    <th>Grading</th><th>Bron</th><th>ms</th><th>Redenen</th>
  </tr></thead>
  <tbody>
    {''.join(row_html(r) for r in rows)}
  </tbody>
</table>
<p style="margin-top:16px;color:#5b6778;font-size:12.5px">
  Lichte smoke-test — geen vervanging voor Ragas/DeepEval. Dit is "werkt het?", niet "hoe goed werkt het?".
</p>
</body></html>
"""
    return HTMLResponse(html)


@router.get("/eval.json", response_class=JSONResponse)
async def eval_json(request: Request):
    """Same eval as /eval but JSON for scripting."""
    entries = _load_golden()
    os_client = request.app.state.opensearch
    redis_client = request.app.state.redis
    out: list[dict] = []
    for entry in entries:
        t0 = time.time()
        try:
            result = await run_crag(
                query=entry["query"],
                security_tier=SecurityTier(entry.get("security_tier", "PUBLIC")),
                session_id="eval",
                os_client=os_client,
                redis_client=redis_client,
            )
            passed, reasons = _check_entry(entry, result)
            out.append({
                "id": entry.get("id"),
                "query": entry["query"],
                "passed": passed,
                "reasons": reasons,
                "ms": (time.time() - t0) * 1000,
                "grading": result.grading_result,
                "source": result.source,
            })
        except Exception as e:
            out.append({"id": entry.get("id"), "passed": False, "reasons": [str(e)]})
    return {"entries": out, "total": len(out), "passed": sum(1 for r in out if r.get("passed"))}
