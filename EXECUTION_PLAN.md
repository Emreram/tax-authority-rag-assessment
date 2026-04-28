# Concrete Uitvoeringsplan — Naar Senior-Niveau

Datum: 2026-04-28. Vervolg op [SENIOR_REVIEW_AND_PLAN.md](SENIOR_REVIEW_AND_PLAN.md).

**Scope:** alle 11 must-haves. **Detail:** taak-voor-taak met exacte file:lines + acceptance criteria.

**Belangrijke ontdekking sinds vorige plan:**
HyDE is **al gewired** in [demo/app/pipeline/retriever.py:96-114](demo/app/pipeline/retriever.py#L96), gegate door `settings.enable_hyde` (default `False` in [config.py:40](demo/app/config.py#L40)). LLM-rerank idem in [retriever.py:166-173](demo/app/pipeline/retriever.py#L166) met `settings.enable_llm_rerank`. Daardoor zijn M4 en deel van M5 véél kleiner dan in de vorige planning ingeschat. Nieuwe totale tijd: **17-22 uur** i.p.v. 22-30.

**Volgorde:** strikt M1 → M11. Elk item heeft:
- ⏱ tijdsinschatting
- **Files** (paden + line-numbers waar precies te wijzigen)
- **Diff-skelet** (wat toevoegen / vervangen — geen complete code, wel structuur)
- **Acceptance criteria** (concrete check, vaak een `curl`-commando of UI-actie)

Aan het einde staat een **PR-bundel-strategie** zodat dit niet één megablob wordt.

---

## Sprint 1 — Bewijs (8-11 uur)

### M1. Echte Ragas-eval pipeline ⏱ 4-6 uur

**Doel:** vervang de 7 gestubde metric-cards op de Kwaliteit-tab door echte Ragas/DeepEval-runs op de golden-set.

#### M1.1 — Dependencies toevoegen ⏱ 5 min

**File:** [demo/requirements-demo.txt](demo/requirements-demo.txt) (huidige inhoud staat hieronder; voeg 2 regels toe **vóór** regel 18 `pypdf>=4.2.0`)

```
ragas>=0.2.6
deepeval>=1.5.0
```

**Check:** `docker compose build api` voltooit zonder pip-conflict. Geen runtime-check nodig hier.

#### M1.2 — Ragas wrapper module ⏱ 90 min

**File:** nieuwe file `demo/app/eval/__init__.py` (leeg) en `demo/app/eval/ragas_runner.py`.

**Diff-skelet voor `ragas_runner.py`:**

```python
"""
Ragas evaluator — runs context_recall, faithfulness, answer_relevancy
on the golden set using the same Gemma 4 model that powers the demo
generator. Production would use GPT-4 as external judge.
"""
from __future__ import annotations
import asyncio, time
from typing import Optional
from openai import AsyncOpenAI
from ragas.metrics import (
    context_recall, faithfulness, answer_relevancy,
)
from ragas.dataset_schema import SingleTurnSample
from ragas.llms.base import BaseRagasLLM
from ragas.embeddings.base import BaseRagasEmbeddings
from app.config import get_settings
from app.pipeline.embedder import embed_query
from app.pipeline.crag import run_crag  # bestaande helper, zie crag.py
from app.models import SecurityTier

# Adapter-classes om Ragas met onze openai-client te laten werken
class _LocalRagasLLM(BaseRagasLLM):
    def __init__(self, client, model): self.c, self.m = client, model
    async def agenerate_text(self, prompt, **kwargs) -> str:
        r = await self.c.chat.completions.create(
            model=self.m, messages=[{"role": "user", "content": prompt}],
            temperature=0.0, max_tokens=512,
        )
        return r.choices[0].message.content or ""
    # synchronous fallback Ragas may call
    def generate_text(self, prompt, **kwargs):
        return asyncio.run(self.agenerate_text(prompt, **kwargs))

class _LocalRagasEmbeddings(BaseRagasEmbeddings):
    async def aembed_query(self, t): return await embed_query(t)
    async def aembed_documents(self, ts): return [await embed_query(t) for t in ts]

async def run_ragas(entries: list[dict], os_client, redis_client) -> dict:
    """Returns aggregated metrics across the golden set."""
    s = get_settings()
    client = AsyncOpenAI(base_url=s.llm_base_url, api_key="not-needed", timeout=300)
    llm = _LocalRagasLLM(client, s.llm_model)
    emb = _LocalRagasEmbeddings()

    samples, t_start = [], time.time()
    for entry in entries:
        if entry.get("must_refuse"):
            continue  # Ragas niet bedoeld voor refuse-cases
        result = await run_crag(
            query=entry["query"],
            security_tier=SecurityTier(entry.get("security_tier", "PUBLIC")),
            session_id="ragas",
            os_client=os_client, redis_client=redis_client,
        )
        ground_truth = " ".join(entry.get("expected_answer_contains", []))
        samples.append(SingleTurnSample(
            user_input=entry["query"],
            response=result.response or "",
            retrieved_contexts=[c.chunk_text for c in (result.citations or [])],
            reference=ground_truth,
        ))
    # Aggregate: average each metric over samples
    metrics = {}
    for metric_cls, key in [(context_recall, "context_recall"),
                             (faithfulness, "faithfulness"),
                             (answer_relevancy, "answer_relevancy")]:
        metric = metric_cls(llm=llm, embeddings=emb)
        scores = []
        for s_ in samples:
            try: scores.append(await metric.single_turn_ascore(s_))
            except Exception: pass
        metrics[key] = sum(scores) / len(scores) if scores else None
    metrics["sample_count"] = len(samples)
    metrics["duration_s"] = time.time() - t_start
    return metrics
```

**Toelichting:** Ragas roept de LLM intern aan voor scoring. We hergebruiken onze bestaande Gemma-client. `run_crag` is de helper-functie die het eval-dashboard ook al gebruikt ([eval_dashboard.py:50-58](demo/app/routers/eval_dashboard.py#L50)).

**Check:** `docker compose exec api python -c "from app.eval.ragas_runner import run_ragas; print('ok')"` → "ok".

#### M1.3 — DeepEval wrapper module ⏱ 60 min

**File:** nieuwe file `demo/app/eval/deepeval_runner.py`.

```python
"""
DeepEval evaluator — hallucination + bias + toxicity per query.
Lighter than Ragas; uses the same local Gemma as judge.
"""
from deepeval.metrics import HallucinationMetric, BiasMetric, ToxicityMetric
from deepeval.test_case import LLMTestCase
from app.config import get_settings
from app.pipeline.crag import run_crag
from app.models import SecurityTier
import os

async def run_deepeval(entries: list[dict], os_client, redis_client) -> dict:
    s = get_settings()
    # DeepEval needs OPENAI_API_KEY env to be set; we point it at our local DMR.
    os.environ["OPENAI_API_KEY"] = "not-needed"
    os.environ["OPENAI_API_BASE"] = s.llm_base_url

    rates = {"hallucination": [], "bias": [], "toxicity": []}
    for entry in entries:
        if entry.get("must_refuse"): continue
        result = await run_crag(
            query=entry["query"],
            security_tier=SecurityTier(entry.get("security_tier", "PUBLIC")),
            session_id="deepeval",
            os_client=os_client, redis_client=redis_client,
        )
        tc = LLMTestCase(
            input=entry["query"],
            actual_output=result.response or "",
            context=[c.chunk_text for c in (result.citations or [])],
        )
        for name, MetricCls in [("hallucination", HallucinationMetric),
                                  ("bias", BiasMetric),
                                  ("toxicity", ToxicityMetric)]:
            try:
                m = MetricCls(model=s.llm_model, threshold=0.5, async_mode=False)
                await m.a_measure(tc)
                rates[name].append(m.score or 0.0)
            except Exception: pass
    return {
        k: (sum(v)/len(v) if v else None) for k, v in rates.items()
    }
```

**Risico:** DeepEval is ontworpen voor OpenAI/Anthropic; Gemma als judge kan inconsistente cijfers geven. Mitigatie: documenteer in slide 5 dat we de eigen LLM als judge gebruiken (transparant) — productie zou GPT-4 als externe judge inzetten.

**Check:** `docker compose exec api python -c "from app.eval.deepeval_runner import run_deepeval; print('ok')"` → "ok".

#### M1.4 — POST /v1/eval/run endpoint ⏱ 60 min

**File:** [demo/app/routers/eval_dashboard.py](demo/app/routers/eval_dashboard.py) — voeg endpoint toe **na** de bestaande `eval_dashboard` GET-handler (rond regel 145).

```python
import asyncio
_eval_cache: dict | None = None  # in-memory cache van laatste run

@router.post("/v1/eval/run")
async def run_full_eval(request: Request):
    """Draait Ragas + DeepEval over de golden set en cached het resultaat."""
    global _eval_cache
    entries = _load_golden()
    if not entries:
        return JSONResponse({"error": "no golden set"}, status_code=404)

    os_client = request.app.state.opensearch
    redis_client = request.app.state.redis

    from app.eval.ragas_runner import run_ragas
    from app.eval.deepeval_runner import run_deepeval

    ragas_task = asyncio.create_task(run_ragas(entries, os_client, redis_client))
    deepeval_task = asyncio.create_task(run_deepeval(entries, os_client, redis_client))
    ragas_metrics = await ragas_task
    deepeval_metrics = await deepeval_task

    _eval_cache = {
        "ragas": ragas_metrics,
        "deepeval": deepeval_metrics,
        "ts": time.time(),
        "golden_count": len(entries),
    }
    return JSONResponse(_eval_cache)

@router.get("/v1/eval/latest")
async def get_latest_eval():
    """Geeft de laatste cached run terug, zonder opnieuw te runnen."""
    if _eval_cache is None:
        return JSONResponse({"error": "no run yet"}, status_code=404)
    return JSONResponse(_eval_cache)
```

**Check:**
```bash
curl -X POST http://localhost:8000/v1/eval/run | jq
# → {"ragas": {"context_recall": 0.78, "faithfulness": 0.91, ...},
#    "deepeval": {"hallucination": 0.12, ...}, "golden_count": 25, ...}
```

#### M1.5 — Frontend: vervang stub-metrics ⏱ 60 min

**File:** [demo/app/static/app.js](demo/app/static/app.js) — vervang regels **1270-1292** (de hele `loadEval()` functie).

**Diff-skelet:**

```javascript
async function loadEval() {
  // Probeer eerst de cached laatste run; als die er niet is, toon "nog niet gerund".
  let data = null;
  try {
    const r = await fetch("/v1/eval/latest");
    if (r.ok) data = await r.json();
  } catch {}

  const m = data ? buildLiveMetrics(data) : buildEmptyMetrics();
  $("#eval-metrics").innerHTML = m.map(x => `
    <div class="metric-card">
      <div class="metric-label">${esc(x.label)}</div>
      <div class="metric-value ${x.cls}">${esc(x.value)}</div>
      <div class="text-[11px] text-slate-400">${esc(x.hint)}</div>
    </div>`).join("");

  if (data) {
    renderGate(data);
    $("#eval-runner").innerHTML = `<div class="text-xs text-slate-400">
      Laatste run: ${new Date(data.ts*1000).toLocaleString()} · ${data.golden_count} queries · ${Math.round(data.ragas?.duration_s||0)}s</div>`;
  } else {
    renderGate(null);
    $("#eval-runner").innerHTML = `<div class="text-xs text-slate-400">
      Geen run beschikbaar. Klik "Run" om de golden set door de pipeline te sturen.</div>`;
  }
}

function buildLiveMetrics(d) {
  const r = d.ragas || {}, e = d.deepeval || {};
  const colorize = (v, threshold, inverted=false) =>
    v == null ? "warn"
    : inverted ? (v <= threshold ? "good" : v <= threshold*1.5 ? "warn" : "bad")
    : (v >= threshold ? "good" : v >= threshold*0.85 ? "warn" : "bad");
  const fmt = v => v == null ? "—" : (v).toFixed(2);
  return [
    {label: "Faithfulness",      value: fmt(r.faithfulness),      cls: colorize(r.faithfulness, 0.90),         hint: "Ragas · claim is gegrond in context"},
    {label: "Context Recall",    value: fmt(r.context_recall),    cls: colorize(r.context_recall, 0.85),       hint: "Ragas · golden chunks retrieved"},
    {label: "Answer Relevancy",  value: fmt(r.answer_relevancy),  cls: colorize(r.answer_relevancy, 0.85),     hint: "Ragas · semantiek vs vraag"},
    {label: "Hallucination",     value: fmt(e.hallucination),     cls: colorize(e.hallucination, 0.10, true),  hint: "DeepEval · fabricatie-rate"},
    {label: "Bias",              value: fmt(e.bias),              cls: colorize(e.bias, 0.10, true),           hint: "DeepEval · demografische drift"},
    {label: "Toxicity",          value: fmt(e.toxicity),          cls: colorize(e.toxicity, 0.05, true),       hint: "DeepEval · PII/schade"},
  ];
}

function buildEmptyMetrics() {
  return ["Faithfulness","Context Recall","Answer Relevancy","Hallucination","Bias","Toxicity"]
    .map(label => ({label, value: "—", cls: "warn", hint: "Nog geen run"}));
}
```

Update `renderGate(d)` (regel **1316**) zodat hij `null` accepteert en de bestaande pass/fail logic koppelt aan echte cijfers:

```javascript
function renderGate(d) {
  const r = d?.ragas || {};
  const stages = [
    {name:"Retrieval Quality",  req:"Context Recall ≥ 0.85", pass: (r.context_recall ?? 0) >= 0.85},
    {name:"Generation Quality", req:"Faithfulness ≥ 0.90",   pass: (r.faithfulness ?? 0)   >= 0.90},
    {name:"Answer Quality",     req:"Answer Relevancy ≥ 0.85", pass: (r.answer_relevancy ?? 0) >= 0.85},
    {name:"Safety",             req:"Hallucination ≤ 0.10",  pass: (d?.deepeval?.hallucination ?? 1) <= 0.10},
  ];
  const ship = d ? stages.every(s => s.pass) : false;
  $("#eval-gate").innerHTML = `…` // (zelfde als nu, gebruik stages)
}
```

**Update `runGoldenSet()` (regel 1294-1314)** — vervang `/eval.json` fetch door `POST /v1/eval/run`:

```javascript
async function runGoldenSet() {
  const host = $("#eval-runner");
  renderLoading(host, { rows: 5, text: "Golden set draait via Ragas + DeepEval — kan 1-2 minuten duren op CPU" });
  try {
    const r = await fetch("/v1/eval/run", { method: "POST" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const d = await r.json();
    await loadEval();  // herlaad UI met nieuwe data
  } catch (err) {
    renderError(host, { err, onRetry: runGoldenSet });
  }
}
```

#### M1.6 — Golden set uitbreiden van 5 → 25 entries ⏱ 60 min

**File:** [eval/golden_test_set_spec.json](eval/golden_test_set_spec.json) — voeg 20 entries toe in de `entries` array (regel 30-104), volgens de schema die op regel 16-28 staat.

Distributie:
- 8× SIMPLE/Dutch (uitbreiding van gs-001 type)
- 6× COMPLEX (uitbreiding van gs-003 type)
- 5× REFERENCE (ECLI + Artikel-nummers, uitbreiding van gs-002)
- 3× ADVERSARIAL (1 out_of_scope, 1 cross_tier_leak, 1 temporal_trap)
- 3× CROSS-TIER permutaties (PUBLIC vs RESTRICTED vs FIOD voor zelfde query)

Zorg dat `expected_chunk_ids` matcht met chunk_ids in [demo/seed_data/chunks.json](demo/seed_data/chunks.json) — anders faalt context_recall structureel.

**Check:** `python -c "import json; d=json.load(open('eval/golden_test_set_spec.json')); print(len(d['entries']))"` → 25.

#### M1.7 — Acceptance van M1 (full)

```bash
# 1. Build + start
docker compose down && docker compose up -d --build
# 2. Wacht warmup
until curl -sf http://localhost:8000/health/detailed | grep -q warmup_complete; do sleep 2; done
# 3. Trigger run
curl -X POST http://localhost:8000/v1/eval/run | jq '.ragas, .deepeval'
# Verwachting: alle 9 metrics zijn floats (geen null, geen stub).
# 4. Open http://localhost:8000/#eval — zes metric-cards tonen echte cijfers.
# 5. Geen "synthetic"-badge meer zichtbaar.
```

---

### M2. TTFT-meting + per-turn badge ⏱ 2-3 uur

#### M2.1 — Backend: emit ttft event ⏱ 45 min

**File:** [demo/app/routers/chat.py](demo/app/routers/chat.py).

**Wijziging 1:** voeg `t_first_token` tracking toe vlak na regel **84** (`t_total = time.time()`):

```python
t_total = time.time()
t_first_token = None  # NEW
```

**Wijziging 2:** in cache-HIT pad, na regel **101** (`yield _sse("trace", {"node": "cache_lookup", "result": "HIT", ...`), emit ttft:

```python
ttft_ms = (time.time() - t_total) * 1000
yield _sse("ttft", {"ms": round(ttft_ms, 1), "source": "cache"})
```

**Wijziging 3:** in generate-stream pad, regel **214** (`async for token in generate_stream(...)`), wrap zodat eerste token ttft emit:

```python
async for token in generate_stream(GENERATOR_SYSTEM, user_prompt, ...):
    if t_first_token is None:
        t_first_token = time.time()
        ttft_ms = (t_first_token - t_total) * 1000
        yield _sse("ttft", {"ms": round(ttft_ms, 1), "source": "pipeline"})
    full_text += token
    yield _sse("token", token)
```

**Wijziging 4:** in refuse-pad, vóór regel **161** (`for piece in _split_for_stream(refuse_text)`), emit ttft:

```python
ttft_ms = (time.time() - t_total) * 1000
yield _sse("ttft", {"ms": round(ttft_ms, 1), "source": "refuse"})
```

**Check:**
```bash
curl -N -X POST http://localhost:8000/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"query":"Wat is de arbeidskorting in 2024?","security_tier":"PUBLIC"}' \
  | head -20
# Verwacht: een "event: ttft\ndata: {\"ms\":287.3,\"source\":\"pipeline\"}" in de eerste 20 SSE-blocks.
```

#### M2.2 — Frontend: TTFT badge per turn ⏱ 60 min

**File:** [demo/app/static/app.js](demo/app/static/app.js).

**Wijziging 1:** in `addAsstPlaceholder()` (rond regel 490) — voeg een `<div class="ttft-badge" hidden></div>` toe in het `innerHTML`-template, vlak na `<div class="msg-role">KennisAssistent</div>`:

```html
<div class="msg-role">KennisAssistent</div>
<div class="ttft-badge" hidden></div>
```

En registreer `ttftBadge: el.querySelector(".ttft-badge")` in het `asst`-object dat returned wordt (rond regel 524).

**Wijziging 2:** in `handleChatEvent` (regel 668), voeg een case toe:

```javascript
case "ttft": renderTtftBadge(asst, data); break;
```

**Wijziging 3:** voeg de helper toe (b.v. na `renderParentBadge` op regel 633):

```javascript
function renderTtftBadge(asst, {ms, source}) {
  if (!asst.ttftBadge) return;
  const cls = ms <= 500 ? "good" : ms <= 1500 ? "warn" : "bad";
  const sourceLabel = source === "cache" ? "via cache" : source === "refuse" ? "via refuse" : "live";
  asst.ttftBadge.className = `ttft-badge ${cls}`;
  asst.ttftBadge.innerHTML = `TTFT <strong>${ms.toFixed(0)} ms</strong> · drempel 1500 ms · ${sourceLabel}`;
  asst.ttftBadge.hidden = false;
}
```

**Wijziging 4:** voeg styles toe in [demo/app/static/theme.css](demo/app/static/theme.css):

```css
.ttft-badge {
  display: inline-block;
  font-size: 11px;
  padding: 3px 9px;
  border-radius: var(--r-pill);
  margin-bottom: 8px;
  font-family: var(--font-mono);
  border: 1px solid var(--border);
}
.ttft-badge.good { background: rgba(34,197,94,.15); color: var(--ok); border-color: rgba(34,197,94,.4); }
.ttft-badge.warn { background: rgba(245,158,11,.15); color: var(--warn); border-color: rgba(245,158,11,.4); }
.ttft-badge.bad  { background: rgba(239,68,68,.15); color: var(--err); border-color: rgba(239,68,68,.4); }
```

**Wijziging 5:** asset-versies bumpen — verander `?v=15` naar `?v=16` in [demo/app/static/index.html](demo/app/static/index.html) (3 plekken).

#### M2.3 — Acceptance van M2

1. Open http://localhost:8000 met hard refresh
2. Stel een vraag → onder "KennisAssistent" verschijnt **vóór de eerste token** een groen/amber/rood pill: `TTFT 287 ms · drempel 1500 ms · live`
3. Herhaal vraag → zelfde pill verschijnt met `via cache` en cijfer < 50 ms (groen)
4. Stel adversarial vraag → pill verschijnt met `via refuse`

---

### M3. Bigger seed corpus ⏱ 1-2 uur

#### M3.1 — Verzamel 8 publieke Nederlandse fiscale documenten ⏱ 30 min

Bronnen (alleen publieke):
1. https://wetten.overheid.nl — Wet IB 2001, Hoofdstuk 3 (uitbreiding bestaande set)
2. https://wetten.overheid.nl — Wet OB 1968, Hoofdstuk II (BTW)
3. https://wetten.overheid.nl — Algemene wet inzake rijksbelastingen, art 67
4. https://uitspraken.rechtspraak.nl — 2 recente Hoge Raad uitspraken (laatste 12 maanden)
5. https://www.belastingdienst.nl — 1 publieke beleidsmemo over arbeidskorting
6. Verzonnen interne FIOD-procedurehandleiding (1 doc, gemarkeerd `CLASSIFIED_FIOD`) — 100% fictioneel, voor RBAC-demo

**Plaats:** `demo/seed_data/pdfs/` — 8 PDFs, totaal ≤ 5 MB. Geen scans (slechte OCR), alleen text-PDFs.

#### M3.2 — Pre-ingest script ⏱ 45 min

**File:** nieuwe file `demo/scripts/preingest.sh`.

```bash
#!/usr/bin/env bash
# Pre-ingest seed PDFs into OpenSearch on first startup.
# Idempotent — checks if doc-count > threshold first.
set -euo pipefail
cd "$(dirname "$0")/.."

EXPECTED_DOCS=8
COUNT=$(curl -sf http://api:8000/v1/documents 2>/dev/null \
  | python -c "import json,sys; d=json.load(sys.stdin); print(len(d.get('documents',[])))" \
  || echo 0)

if [ "$COUNT" -ge "$EXPECTED_DOCS" ]; then
  echo "preingest: $COUNT docs already indexed, skipping"
  exit 0
fi

for pdf in seed_data/pdfs/*.pdf; do
  name=$(basename "$pdf" .pdf)
  tier="PUBLIC"
  case "$name" in
    fiod_*) tier="CLASSIFIED_FIOD" ;;
    intern_*) tier="INTERNAL" ;;
    inspecteur_*) tier="RESTRICTED" ;;
  esac
  echo "preingest: $name ($tier)"
  curl -sf -X POST http://api:8000/v1/ingest \
    -F "file=@$pdf" \
    -F "title=$name" \
    -F "security_classification=$tier" \
    -F "doc_type=publication" > /dev/null || echo "  FAIL: $name"
done
echo "preingest: done"
```

Naming convention voor tier-binding via filename prefix: `fiod_*.pdf`, `intern_*.pdf`, `inspecteur_*.pdf`, `*.pdf` (default PUBLIC).

#### M3.3 — Compose-integratie ⏱ 30 min

**File:** [demo/docker-compose.yml](demo/docker-compose.yml) — voeg een **init-container** toe na de `api` service:

```yaml
  preingest:
    image: curlimages/curl:8.10.0
    depends_on:
      api: { condition: service_healthy }
    volumes:
      - ./scripts:/scripts:ro
      - ./seed_data:/seed_data:ro
    entrypoint: ["sh","-c","sh /scripts/preingest.sh"]
    restart: "no"
```

Vereist een **healthcheck** op de `api` service. Voeg toe in dezelfde `api`-block:

```yaml
    healthcheck:
      test: ["CMD-SHELL","curl -sf http://localhost:8000/health/detailed | grep -q '\"warmup_complete\":true'"]
      interval: 10s
      timeout: 5s
      retries: 30
      start_period: 60s
```

#### M3.4 — Acceptance van M3

```bash
docker compose down -v  # wis OpenSearch volume voor schone test
docker compose up -d --build
# Wacht 90s — preingest moet vanzelf draaien
sleep 90
curl -sf http://localhost:8000/v1/documents | jq '.documents | length'
# Verwacht: 8
curl -sf "http://localhost:8000/v1/cache/entries?tier=PUBLIC" | jq
# Open http://localhost:8000/#documents — 8 doc-cards zichtbaar, 1 met FIOD-tier
```

---

## Sprint 2 — Compleetheid (4-6 uur)

### M4. HyDE actief in demo ⏱ 60 min

**Bonus van het lezen:** HyDE is **al gewired** in [retriever.py:96-114](demo/app/pipeline/retriever.py#L96). Drie wijzigingen volstaan.

#### M4.1 — Default-flag flippen ⏱ 5 min

**File:** [demo/app/config.py:40](demo/app/config.py#L40).

```python
enable_hyde: bool = True  # was: False
```

#### M4.2 — Trace-event vanuit retriever ⏱ 30 min

**File:** [demo/app/pipeline/retriever.py:96-108](demo/app/pipeline/retriever.py#L96) — laat `retrieve()` een **callback** accepteren die SSE-events kan emitten. Voeg parameter toe aan signature (regel 76):

```python
async def retrieve(
    client: OpenSearch,
    query: str,
    security_tier: SecurityTier,
    query_type: str,
    settings,
    on_trace=None,  # NEW: optional async callback
) -> list[dict]:
```

In het HyDE-blok (regel 99-108), na succesvol drafted hypothesis:

```python
if hypothesis:
    hyde_embedding = await _embed_passage(hypothesis)
    if on_trace:
        await on_trace("hyde", {
            "result": "drafted",
            "detail": hypothesis[:80],
            "duration_ms": 0,  # measure with time.time() if you want
        })
    log.info("hyde_drafted", chars=len(hypothesis))
```

#### M4.3 — Chat router gebruikt callback ⏱ 20 min

**File:** [demo/app/routers/chat.py:131](demo/app/routers/chat.py#L131) — vervang de `retrieve()` call:

```python
# was:
retrieved = await retrieve(os_client, current_query, tier, query_type, settings)

# wordt:
async def _trace(node, payload):
    nonlocal_emit_queue.append(_sse(node, payload))  # see note below
retrieved = await retrieve(os_client, current_query, tier, query_type, settings, on_trace=_trace)
for evt in nonlocal_emit_queue:
    yield evt
nonlocal_emit_queue.clear()
```

(Generators kunnen geen `yield` vanuit een nested function. Simpelste oplossing: laat `retrieve()` een `list[dict]` aan trace-events teruggeven naast de hits, of gebruik een list-buffer zoals hierboven.)

**Alternatief (cleaner):** verander `retrieve()` retourtype naar `tuple[list[dict], list[dict]]` waar tweede list `trace_events` is:

```python
# in retriever.py
trace_events: list[dict] = []
if hypothesis: trace_events.append({"node":"hyde","detail":hypothesis[:80]})
...
return fused[: settings.top_k_rerank], trace_events
```

**In chat.py:131:**
```python
retrieved, retrieve_traces = await retrieve(...)
for t in retrieve_traces:
    yield _sse("trace", {**t, "duration_ms": 0})
```

#### M4.4 — CRAG-state-diagram update ⏱ 10 min

**File:** [demo/app/static/app.js](demo/app/static/app.js) — zoek de state-flow array (rond regel 1291 in vorige versie, controleer in current state):

```javascript
const flow = [
  ["cache_lookup", "classify_query", "hyde", "retrieve", "grade_context"],  // hyde toegevoegd
  ["rewrite_and_retry", "parent_expansion", "generate", "validate_output"],
  ["respond", "refuse"],
];
```

En in `NODE_LABELS` (regel 561), voeg toe:
```javascript
hyde: { icon: "🎭", label: "HyDE hypothese" },  // bestaat al!
```

#### M4.5 — Acceptance van M4

```bash
curl -N -X POST http://localhost:8000/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"query":"arbeidskorting", "security_tier":"PUBLIC"}' \
  | grep "hyde"
# Verwacht: "event: trace\ndata: {\"node\":\"hyde\", \"detail\":\"...\", ...}"
```

UI: open http://localhost:8000/#crag na een query — diagram toont `hyde` knoop opgelicht in rij 1.

---

### M5. Query decompositie actief ⏱ 2-3 uur

#### M5.1 — Classifier extended om sub_queries te produceren ⏱ 45 min

**File:** [demo/app/pipeline/classifier.py](demo/app/pipeline/classifier.py) — vervang inhoud (huidige is 36 regels).

**Wijziging in signature:** return now `tuple[str, list[str]]` waar tweede element sub-queries is (leeg voor SIMPLE/REFERENCE).

```python
import re, json
from app.pipeline.llm import generate, generate_json

DECOMPOSE_SYSTEM = """Je bent een query-decomposer voor een Nederlandse fiscale RAG.
Splits een complexe vraag in 2-3 onafhankelijke sub-vragen die elk apart te
beantwoorden zijn met retrieval. Geef terug als JSON:
{"sub_queries": ["vraag 1", "vraag 2"]}

Voorbeeld input: "Ik ben ZZP'er met thuiskantoor — wat aftrekken en hoe BTW?"
Output: {"sub_queries":["welke kosten mag een ZZP'er aftrekken voor een thuiskantoor","is een ZZP'er BTW-plichtig over diensten"]}
"""

async def classify_query(query: str) -> tuple[str, list[str]]:
    if ECLI_PATTERN.search(query): return "REFERENCE", []
    if ARTICLE_PATTERN.search(query) and len(query.split()) < 10:
        return "REFERENCE", []
    result = await generate(CLASSIFICATION_SYSTEM, query, temperature=0.0)
    classification = result.strip().upper()
    if classification == "COMPLEX":
        try:
            d = await generate_json(DECOMPOSE_SYSTEM, query, temperature=0.0)
            sub_q = d.get("sub_queries", [])[:3]  # cap op 3
            return "COMPLEX", sub_q
        except Exception:
            return "COMPLEX", []
    return "SIMPLE", []
```

#### M5.2 — Chat router voert sub-queries parallel uit ⏱ 60 min

**File:** [demo/app/routers/chat.py:118-119](demo/app/routers/chat.py#L118).

**Wijziging signature van classify_query call:**
```python
query_type, sub_queries = await classify_query(query)
```

**Direct daarna (vóór retrieve-loop op regel 122):**
```python
if query_type == "COMPLEX" and sub_queries:
    yield _sse("trace", {
        "node": "decompose",
        "result": f"{len(sub_queries)} sub-queries",
        "detail": " | ".join(sub_queries),
        "duration_ms": 0,
    })
    # Parallel retrieval over sub-queries, merge via RRF
    import asyncio
    sub_results = await asyncio.gather(*[
        retrieve(os_client, sq, tier, "SIMPLE", settings)
        for sq in sub_queries
    ])
    # Sub-RRF merge: average rank across sub-queries
    seen = {}
    for sub_hits in sub_results:
        if isinstance(sub_hits, tuple): sub_hits = sub_hits[0]  # M4 retval shape
        for rank, h in enumerate(sub_hits):
            cid = h["chunk_id"]
            score = 1.0 / (60 + rank + 1)
            seen[cid] = seen.get(cid, (0, h))
            seen[cid] = (seen[cid][0] + score, h)
    merged = sorted(seen.values(), key=lambda x: -x[0])[:settings.top_k_rerank]
    retrieved = [m[1] for m in merged]
    # skip de normal retrieve-loop, ga direct door naar grading...
```

**Belangrijke complicatie:** de bestaande retrieve-grade-loop bevat retry-logic. Kies één van twee aanpakken:

A) **Decompositie-pad helemaal naast de loop** (eenvoudig): bij COMPLEX, doe sub-retrieval, sla retry over, ga direct naar grade.
B) **Decompositie ter vervanging van current_query** (consistenter): bouw een gemerged kandidaat-set en gebruik die i.p.v. de single-query retrieve in de loop.

**Aanbeveling:** A — leesbaarder, retry voor COMPLEX bracht in praktijk weinig op.

#### M5.3 — CRAG-state-diagram + node-label ⏱ 15 min

**File:** [demo/app/static/app.js](demo/app/static/app.js) — voeg toe in flow-array:
```javascript
["cache_lookup", "classify_query", "decompose", "hyde", "retrieve", "grade_context"]
```

In `NODE_LABELS`:
```javascript
decompose: { icon: "🪓", label: "Vraag splitsen" }
```

#### M5.4 — Acceptance van M5

```bash
curl -N -X POST http://localhost:8000/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"query":"Ik ben ZZP'\''er met thuiskantoor wat kan ik aftrekken en moet ik BTW heffen?","security_tier":"PUBLIC"}' \
  | grep -E "decompose|sub_queries"
# Verwacht: een trace-event met node "decompose" en detail die 2 sub-queries laat zien.
```

UI: stel deze vraag in chat → in CRAG-diagram licht `decompose`-knoop op vóór `retrieve`.

---

### M9. Top-K config terug in Retrieval-tab ⏱ 30 min

#### M9.1 — DOM aanpassing ⏱ 10 min

**File:** [demo/app/static/index.html](demo/app/static/index.html) — in de Retrieval-section, **na** de `<div id="retrieval-timings">` (vóór de `<div class="grid md:grid-cols-2 gap-4">` met de rivers), voeg toe:

```html
<div class="flex flex-wrap gap-2 text-[11px] font-mono text-slate-400" id="retrieval-config-pills"></div>
```

#### M9.2 — Render-functie in app.js ⏱ 15 min

**File:** [demo/app/static/app.js](demo/app/static/app.js) — in `renderRetrievalTrace(d)` (rond regel 1197), voeg toe:

```javascript
const c = d.config;
$("#retrieval-config-pills").innerHTML = [
  ["BM25 top-k", c.top_k_bm25],
  ["kNN top-k", c.top_k_knn],
  ["RRF k", c.rrf_k],
  ["Rerank top-k", c.top_k_rerank],
].map(([k,v]) => `<span class="pill"><span class="text-slate-500">${k}</span> ${v}</span>`).join("");
```

#### M9.3 — Acceptance van M9

UI: open http://localhost:8000/#retrieval, doe een query → boven de rivers verschijnt: `BM25 top-k 6 · kNN top-k 6 · RRF k 60 · Rerank top-k 5`.

---

## Sprint 3 — Polish & reliability (5-7 uur)

### M6. Quantization-widget op Ingestie ⏱ 90 min

#### M6.1 — Memory-math endpoint ⏱ 30 min

**File:** [demo/app/routers/health.py](demo/app/routers/health.py) of een nieuwe admin-router. Voeg endpoint toe:

```python
@router.get("/v1/admin/index_stats")
async def index_stats(request: Request):
    os_client = request.app.state.opensearch
    s = get_settings()
    stats = os_client.count(index=s.opensearch_index)
    n = stats["count"]
    dim = s.embedding_dim
    overhead = 1.8  # HNSW graph overhead factor
    sizes = {
        "fp32": n * dim * 4 * overhead,
        "fp16": n * dim * 2 * overhead,
        "int8": n * dim * 1 * overhead,
        "pq8":  n * dim * 0.125 * overhead,  # 8x compression with PQ
    }
    return {
        "chunks": n, "dim": dim, "overhead": overhead,
        "current_precision": "fp32",  # OpenSearch default
        "memory_bytes": sizes,
        "production_20m_bytes": {k: 20_000_000 * dim * (sizes[k] / (n*dim*overhead)) for k in sizes} if n > 0 else None,
    }
```

#### M6.2 — UI-widget op Ingestie-pagina ⏱ 60 min

**File:** [demo/app/static/index.html](demo/app/static/index.html) — in de `<section data-workspace="ingest">`, na de hierarchy-tree blok, voeg toe:

```html
<div class="bg-bd-surface border border-bd-border rounded-xl p-5">
  <h2 class="text-lg font-semibold mb-3">Vector-quantization</h2>
  <div id="quant-grid" class="grid grid-cols-2 md:grid-cols-4 gap-3"></div>
  <p class="text-[11px] text-slate-500 mt-3">Productie-projectie bij 20M chunks rechts onderin elke kaart.</p>
</div>
```

In [demo/app/static/app.js](demo/app/static/app.js) — voeg renderQuant toe en koppel aan `setView('ingest')`:

```javascript
async function renderQuant() {
  try {
    const r = await fetch("/v1/admin/index_stats");
    const d = await r.json();
    const fmt = b => b<1e6 ? (b/1024).toFixed(1)+" KB" : b<1e9 ? (b/1e6).toFixed(1)+" MB" : (b/1e9).toFixed(2)+" GB";
    const cards = ["fp32","fp16","int8","pq8"].map(prec => `
      <div class="border border-bd-border rounded p-3 ${prec===d.current_precision?'bg-bd-orange/10 border-bd-orange':''}">
        <div class="text-xs text-slate-400 mb-1">${prec.toUpperCase()}</div>
        <div class="font-mono text-lg">${fmt(d.memory_bytes[prec])}</div>
        <div class="text-[10px] text-slate-500 mt-1">@ 20M: ${fmt(d.production_20m_bytes?.[prec] || 0)}</div>
      </div>`);
    $("#quant-grid").innerHTML = cards.join("");
  } catch {}
}
// in setView() — bij view==='ingest', ook renderQuant() aanroepen
```

#### M6.3 — Acceptance van M6

UI: open http://localhost:8000/#ingest → onderaan de pagina verschijnt een grid met 4 kaarten (fp32 / fp16 / int8 / pq8) elk met current corpus-grootte + productie-projectie naar 20M chunks. De huidige precision (fp32) is oranje gemarkeerd.

---

### M7. Circuit-breaker rond LLM-calls ⏱ 90 min

#### M7.1 — Breaker-module ⏱ 45 min

**File:** nieuwe file `demo/app/pipeline/breaker.py`.

```python
"""Simple circuit-breaker for the LLM client.
States: CLOSED → (n failures within window) → OPEN → (timeout) → HALF_OPEN → (1 ok) → CLOSED."""
import time
from enum import Enum

class State(str, Enum): CLOSED="CLOSED"; OPEN="OPEN"; HALF_OPEN="HALF_OPEN"

class BreakerOpenError(RuntimeError): pass

class CircuitBreaker:
    def __init__(self, threshold=3, window_s=30, recover_after_s=20):
        self.threshold = threshold
        self.window_s = window_s
        self.recover_after_s = recover_after_s
        self.state = State.CLOSED
        self.failures: list[float] = []
        self.opened_at: float | None = None

    def _gc(self):
        now = time.time()
        self.failures = [t for t in self.failures if now - t < self.window_s]

    def before(self):
        if self.state == State.OPEN:
            if time.time() - (self.opened_at or 0) >= self.recover_after_s:
                self.state = State.HALF_OPEN
            else:
                raise BreakerOpenError("LLM service tijdelijk onbeschikbaar (circuit open)")

    def on_success(self):
        if self.state == State.HALF_OPEN:
            self.state = State.CLOSED
            self.failures.clear()

    def on_failure(self):
        self.failures.append(time.time())
        self._gc()
        if self.state == State.HALF_OPEN or len(self.failures) >= self.threshold:
            self.state = State.OPEN
            self.opened_at = time.time()

# singleton
breaker = CircuitBreaker()
```

#### M7.2 — Wrap LLM-calls ⏱ 30 min

**File:** [demo/app/pipeline/llm.py](demo/app/pipeline/llm.py) — voeg `breaker.before()` aan begin van elke LLM-call (`generate`, `generate_stream`, `generate_json`). Bv. `generate` (regel 39):

```python
from app.pipeline.breaker import breaker
async def generate(...):
    breaker.before()
    s = get_settings()
    try:
        resp = await get_client().chat.completions.create(...)
        breaker.on_success()
        return resp.choices[0].message.content or ""
    except Exception:
        breaker.on_failure()
        raise
```

Doe hetzelfde voor `generate_stream` (regel 61) en `generate_json` (regel 88).

#### M7.3 — Refuse-pad bij BreakerOpen ⏱ 15 min

**File:** [demo/app/routers/chat.py](demo/app/routers/chat.py) — wrap de `_streaming_pipeline` body in try/except (rond regel 308 in `chat_stream`):

```python
async def event_gen():
    try:
        async for evt in _streaming_pipeline(request, body):
            yield evt
    except BreakerOpenError as e:
        yield _sse("trace", {"node":"refuse","result":"BREAKER_OPEN","duration_ms":0})
        yield _sse("token", "Ik kan momenteel geen antwoord genereren — het inferentie-systeem is tijdelijk overbelast. Probeer over enkele minuten opnieuw.")
        yield _sse("done", {"source":"breaker","total_ms":0})
    except Exception as e:
        log.error("chat_stream_error", error=str(e))
        yield {"event":"error","data":json.dumps({"detail":str(e)})}
```

#### M7.4 — Acceptance van M7

```bash
# Stop Model Runner manueel om failure te simuleren — niet triviaal.
# Alternatief: tijdelijk LLM_BASE_URL naar http://localhost:9999 (ongeldig) en restart api.
docker compose stop api
LLM_BASE_URL=http://localhost:9999 docker compose up -d api
# Doe 4 chat-requests achter elkaar:
for i in 1 2 3 4; do
  curl -sN -X POST http://localhost:8000/v1/chat -H 'Content-Type: application/json' \
    -d '{"query":"test"}' | head -5
done
# Vanaf request 4 verwacht: "node:refuse, result:BREAKER_OPEN" zonder LLM-poging.
```

---

### M8. Refuse-flow als feature framen ⏱ 30 min

#### M8.1 — Backend: betere refuse-tekst ⏱ 10 min

**File:** [demo/app/routers/chat.py:157-160](demo/app/routers/chat.py#L157) — vervang `refuse_text`:

```python
refuse_text = (
    "Ik heb geen geverifieerd antwoord op deze vraag binnen jouw toegangsniveau "
    f"(**{tier.value}**). Mogelijke vervolgstappen:\n\n"
    "- Probeer een specifiekere formulering met een wetsartikel of begrip\n"
    "- Overleg met een collega die toegang heeft tot een hogere classificatie\n"
    "- Stel de vraag aan een fiscaal jurist als verificatie nodig is\n\n"
    "Deze interactie is gelogd voor audit-doeleinden."
)
```

(Tier-aware boodschap — concrete tier wordt genoemd zodat de gebruiker weet waarom.)

#### M8.2 — Frontend: amber-styling i.p.v. error-rood ⏱ 15 min

**File:** [demo/app/static/app.js](demo/app/static/app.js) — in `handleChatEvent` op `trace`-case (rond regel 670), wanneer `data.node === "refuse"`, voeg een class toe op de bubble:

```javascript
case "trace":
  if (data.node === "refuse") {
    asst.root.classList.add("msg-refuse");
  }
  addTrace(asst, data);
  break;
```

**File:** [demo/app/static/theme.css](demo/app/static/theme.css) — voeg toe:

```css
.msg.assistant.msg-refuse .msg-content {
  border-left: 3px solid var(--warn);
  background: rgba(245,158,11,.05);
}
.msg.assistant.msg-refuse .msg-content::before {
  content: "Gefilterd antwoord";
  display: block;
  font-size: 10px;
  color: var(--warn);
  font-weight: var(--fw-semi);
  letter-spacing: 0.05em;
  text-transform: uppercase;
  margin-bottom: 6px;
}
```

#### M8.3 — Acceptance van M8

UI: stel `Who built the Eiffel Tower?` of `Welke fraudeonderzoeken lopen er bij FIOD?` (als Publiek) → de refuse-bubble heeft amber border, label "Gefilterd antwoord" bovenaan, en de tekst noemt expliciet je tier-naam.

---

### M10. Audit-trail per query ⏱ 90 min

#### M10.1 — Audit module ⏱ 45 min

**File:** nieuwe file `demo/app/audit.py`.

```python
"""Per-query audit-trail in Redis sorted set per dag."""
import time, json, structlog
log = structlog.get_logger()

async def log_query(redis_client, *, session_id: str, tier: str, query: str,
                    grade: str, citations: list[str], ttft_ms: float | None,
                    source: str):
    ts = time.time()
    record = {
        "ts": ts, "session_id": session_id, "tier": tier,
        "query": query[:500], "grade": grade,
        "citations": citations[:10], "ttft_ms": ttft_ms, "source": source,
    }
    day = time.strftime("%Y-%m-%d", time.gmtime(ts))
    key = f"audit:{day}"
    try:
        await redis_client.zadd(key, {json.dumps(record): ts})
        await redis_client.expire(key, 7*24*3600)  # 7 dagen
    except Exception as e:
        log.warning("audit_log_failed", error=str(e))

async def list_recent(redis_client, *, day: str = None, limit: int = 50):
    day = day or time.strftime("%Y-%m-%d", time.gmtime())
    key = f"audit:{day}"
    try:
        rows = await redis_client.zrevrange(key, 0, limit-1)
        return [json.loads(r) for r in rows]
    except Exception:
        return []
```

#### M10.2 — Wire in chat.py ⏱ 20 min

**File:** [demo/app/routers/chat.py](demo/app/routers/chat.py) — bij elk `done`-event (regels 112, 166, 291), roep audit aan:

```python
from app.audit import log_query
# vóór yield _sse("done", ...):
await log_query(redis_client,
    session_id=session_id, tier=tier.value, query=body.query,
    grade=grading_result if 'grading_result' in dir() else "N/A",
    citations=[c["chunk_id"] for c in citations_out] if 'citations_out' in dir() else [],
    ttft_ms=None,  # TODO koppel met M2 ttft tracking
    source=("cache" if cached else "refuse" if grading_result != "RELEVANT" else "pipeline"),
)
```

#### M10.3 — Endpoint + UI-tabel ⏱ 25 min

**File:** [demo/app/routers/cache.py](demo/app/routers/cache.py) of nieuwe `audit.py` router. Voeg endpoint toe:

```python
@router.get("/v1/audit/recent")
async def audit_recent(request: Request):
    from app.audit import list_recent
    rows = await list_recent(request.app.state.redis, limit=50)
    return {"entries": rows}
```

**File:** [demo/app/static/index.html](demo/app/static/index.html) — in `<section data-workspace="security">`, na het Cache-blok, voeg toe:

```html
<div class="bg-bd-surface border border-bd-border rounded-xl p-5">
  <h3 class="text-sm font-bold tracking-wide text-bd-navy mb-3">Audit-trail (laatste 50)</h3>
  <div id="audit-table" class="text-xs space-y-1"></div>
</div>
```

In [demo/app/static/app.js](demo/app/static/app.js), voeg toe en koppel aan `setView('security')`:

```javascript
async function renderAudit() {
  try {
    const r = await fetch("/v1/audit/recent");
    const d = await r.json();
    if (!d.entries?.length) {
      $("#audit-table").innerHTML = `<div class="text-slate-500">Nog geen queries gelogd vandaag.</div>`;
      return;
    }
    $("#audit-table").innerHTML = d.entries.map(e => `
      <div class="flex gap-2 font-mono py-1 border-b border-bd-border">
        <span class="text-slate-500">${new Date(e.ts*1000).toLocaleTimeString()}</span>
        <span class="pill pill-tier">${esc(e.tier)}</span>
        <span class="flex-1 truncate">${esc(e.query)}</span>
        <span class="${e.grade==='RELEVANT'?'text-bd-green':e.grade==='IRRELEVANT'?'text-bd-red':'text-bd-amber'}">${esc(e.grade||'')}</span>
      </div>`).join("");
  } catch {}
}
```

#### M10.4 — Acceptance van M10

```bash
# Stel 3 queries (verschillende tiers)
# Open http://localhost:8000/#security — onderaan tabel met 3 rijen, elk met
# tijd / tier-pill / query / grade-kleur.
curl -sf http://localhost:8000/v1/audit/recent | jq '.entries | length'
# >= 3
```

---

## Sprint 4 — Demo-paraatheid (1-2 uur)

### M11. Dress-rehearsal + screencast ⏱ 90 min

**Stappen** (geen code):

1. **Stack opnieuw vanaf nul:** `docker compose down -v && docker compose up -d --build` — wacht warmup.
2. **Pre-warm cache:** voer 8 demo-queries uit (één per archetype):
   - "Wat is de arbeidskorting in 2024?"
   - "ECLI:NL:HR:2023:1234"
   - "Ik ben ZZP'er met thuiskantoor wat aftrekken en BTW?"
   - "arbeidskorting" (HyDE-trigger — kort)
   - "Welke fraudeonderzoeken lopen bij FIOD?" (refuse)
   - "Who built the Eiffel Tower?" (out-of-scope)
   - "Wat is de hypotheekrenteaftrek?" (decompositie-trigger)
   - "Wat is de arbeidskorting?" (cache HIT op 1)
3. **Run Ragas:** klik "Run" op Kwaliteit-tab; noteer expected metrics.
4. **OBS / ScreenToGif starten** — opname van het volledige scherm op 1920×1080.
5. **Volg de demo-storyline uit [SENIOR_REVIEW_AND_PLAN.md §3.8](SENIOR_REVIEW_AND_PLAN.md):** 8 acts in ~9 minuten.
6. **Stop opname**, opslaan als `demo/recordings/dress_rehearsal_v3_<datum>.mp4`.
7. **Bekijk terug.** Punten waarop je hapert of waar UI iets onverwacht doet → noteren als kleine fixes (in plaats van re-record).
8. **Optioneel:** als de eerste rehearsal te haperig is, herhaal stap 4-7 één keer.
9. **`.gitignore` of `git lfs`:** als de mp4 > 50MB, niet in git committen — externe link/cloud.

#### M11 Acceptance

- Bestand `demo/recordings/dress_rehearsal_v3_*.mp4` bestaat
- Doorlopen video < 10 minuten
- Geen blanke schermen, geen 500 errors zichtbaar
- Alle 6-8 acts uit de storyline gedemonstreerd

---

## PR / commit-bundel-strategie

Niet alles in één commit. Voorgesteld:

| PR | Inhoud | Welke M-items |
|---|---|---|
| **PR-1: Sprint 1 Bewijs** | Ragas/DeepEval modules + endpoint + UI + golden expansion + TTFT + bigger corpus + preingest | M1, M2, M3 |
| **PR-2: Sprint 2 Compleetheid** | HyDE flag + decompositie + Top-K pill | M4, M5, M9 |
| **PR-3: Sprint 3 Polish** | Quantization-widget, breaker, refuse-framing, audit-trail | M6, M7, M8, M10 |
| **PR-4: Demo-paraat** | Screencast + DEMO_SCRIPT update om M1-M10 features op te nemen | M11 |

Elke PR moet groen door de bestaande golden-set runnen vóór merge.

## Master timeline

| Dag | Sprint | Hours | Cumulatief |
|---|---|---|---|
| Dag 1 (4-5h) | Sprint 1: M1.1-M1.4 (Ragas backend) | 4 | 4 |
| Dag 2 (4h) | Sprint 1: M1.5-M1.7 + M2 (UI + TTFT) | 4 | 8 |
| Dag 3 (3h) | Sprint 1: M3 (corpus) + Sprint 2: M4 | 3 | 11 |
| Dag 4 (3h) | Sprint 2: M5 + M9 | 3 | 14 |
| Dag 5 (3h) | Sprint 3: M6 + M7 | 3 | 17 |
| Dag 6 (3h) | Sprint 3: M8 + M10 | 3 | 20 |
| Dag 7 (1.5h) | Sprint 4: M11 dress-rehearsal | 1.5 | 21.5 |

Totaal: **~21.5 uur**, één werkweek.

## Risico's tijdens uitvoer

| Risico | Indicator | Mitigatie |
|---|---|---|
| Ragas-imports faalt op Python 3.11 wegens dependency-pin | `pip install` faalt in Dockerfile | Pin `ragas==0.2.10` (laatst bekend werkende voor Python 3.11) of gebruik `ragas==0.1.21` als fallback |
| Gemma 4 als Ragas-judge produceert onstabiele cijfers | Run-to-run variantie > 0.1 op faithfulness | Pin temperature=0; documenteer in slide 5 dat productie GPT-4 als external judge gebruikt; geef de min/max range als interval i.p.v. single number |
| `run_crag` helper bestaat niet (alleen `_streaming_pipeline`) | ImportError in ragas_runner | Bouw `run_crag` als non-streaming wrapper rond `_streaming_pipeline` (verzamel events, return `Result`-namedtuple); zie eval_dashboard.py voor signatuur |
| pdfplumber/pypdf parsen Wetten.overheid PDFs slecht | Chunks zijn 1 woord per stuk | Test op één PDF eerst; als PDF "lossy" is, vervang door HTML-source van wetten.overheid.nl |
| Decompositie produceert 1 sub-query (geen splitsing) | gs-003 retrieval-recall blijft laag | Hard-coden minimaal 2 sub-queries OF schakel de feature uit voor die specifieke query in golden-set |
| Circuit-breaker wordt nooit getriggered tijdens demo | Live demo toont nooit het BreakerOpen-pad | Maak een Operations → Toegang knop "Test breaker" die kunstmatig 3 failures forceert (voor demo-doeleinden) |
| TTFT > 1500ms op koude eerste call tijdens demo | Pill is rood ipv groen | Pre-warm in pre-flight (zie M11 stap 2); demo doet GEEN cold start |

## Final checklist (1 dag voor)

- [ ] Alle 4 PRs gemerged in master
- [ ] `docker compose down -v && docker compose up -d --build` werkt vanaf nul
- [ ] `curl /v1/eval/run` levert echte cijfers (geen `null`)
- [ ] Cache-hit query toont TTFT < 100 ms (groene pill)
- [ ] Pipeline-query op koude cache toont TTFT < 1500 ms (groene of amber pill, niet rood)
- [ ] HyDE-knoop pulst op SIMPLE-query in CRAG-diagram
- [ ] Decompose-knoop pulst op COMPLEX-query in CRAG-diagram
- [ ] 8 documenten in Documenten-tab, 1 met FIOD-tier
- [ ] Audit-trail toont laatste queries met tier-pill
- [ ] Quantization-widget toont 4 kaarten op Ingestie-tab
- [ ] Refuse-flow heeft amber border + "Gefilterd antwoord" label
- [ ] Dress-rehearsal mp4 op 2e device beschikbaar
- [ ] Slide-deck `assessment_AI_USE_emresemerci.pptx` + `slides/output/operations_justification.pptx` integreerd
- [ ] WiFi uit; demo werkt offline
- [ ] [DEMO_SCRIPT.md](demo/DEMO_SCRIPT.md) is bijgewerkt met de nieuwe acts (HyDE, decompose, Ragas-run)

---

## Wat NIET in dit plan zit

Bewust uitgesloten (zie [SENIOR_REVIEW_AND_PLAN.md §3.11](SENIOR_REVIEW_AND_PLAN.md)):
- vLLM/Mixtral integratie
- Multi-node OpenSearch cluster
- Cross-encoder reranker als runtime
- JWT/OIDC auth
- OpenTelemetry/Grafana stack

Plus uit dit concrete plan **óók niet**:
- **Stress-test mode** (50 concurrent queries) — was nice-to-have; voegt veel complexiteit toe voor één demo-moment. Skip tenzij je >25u tijd hebt.
- **Cosine threshold-tuning slider** — slide 4 van het deck dekt dit verbaal; UI-experiment kost 2u zonder veel ROI.
- **ECLI-shortcut visible in UI** — backend doet dit al via `_exact_id_search` ([retriever.py:49](demo/app/pipeline/retriever.py#L49)); zichtbaarheid in trace is een 30-min-optie als tijd over is.

---

## Wanneer dit klaar is

Na PR-4 zie je het volgende verschil:

**Tim opent http://localhost:8000.** Hij stelt zijn eerste vraag. Onder het antwoord staat **"TTFT 287ms ✓"** — letterlijke beantwoording van de assessment-eis. Hij klikt door naar Operations → Kwaliteit en ziet **echte Ragas-getallen**: faithfulness 0.91, context_recall 0.84. Hij klikt "Run" — 90 seconden later updaten ze live, ship/hold-pills springen op groen. Hij switcht naar Toegang en ziet **een audit-trail-tabel** met zijn eigen queries van de afgelopen 3 minuten. Hij switcht naar Ingestie en ziet **8 echte Dutch-tax PDFs** in de structuur, plus een quantization-widget die toont dat dit corpus 64.8 KB is en op 20M chunks ~51 GB zou worden bij fp32.

Dit is het verschil tussen "leuk prototype" en "ja, die heeft het begrepen". 21 uur werk, in één werkweek doenbaar.
