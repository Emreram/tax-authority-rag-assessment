# Reliability & Error-Handling Improvement Plan

Datum: 2026-04-28. Doel: runtime errors **minimaliseren**, en als ze gebeuren **fail-safe, expliciet en professioneel**. Geen weakening van zero-hallucination, RBAC, citation-validation, CRAG-grading, of offline inference.

Tijdsbudget: **~20-30 uur** (volledig professioneel hardening — Sprint S1 t/m S5).

---

# Deel 1 — Failure-mode analyse per gebied

Per gebied: **failure mode**, **huidige gedrag**, **gebruikersimpact**, **demo-risico**, **assessment-risico**, **fail-open / fail-closed / crash**, **aanbeveling**.

## 1.1 Chat streaming / SSE — `demo/app/routers/chat.py`

| Aspect | Detail |
|---|---|
| Failure mode | Mid-stream LLM disconnect, klant-side network drop, JSON parse failure op token, generator-exception in any node |
| Huidige gedrag | [chat.py:308-322](demo/app/routers/chat.py#L308) catcht `BreakerOpenError` (M7) en generic `Exception` → emit `{event:"error", data:{detail:str(e)}}`. Stack trace wordt **wel** als `str(e)` aan client doorgegeven. |
| Gebruikersimpact | Verbinding stopt; toast "Fout bij generatie" + raw error-string |
| Demo-risico | Hoog — als grader een `JSONDecodeError` produceert (Gemma 4 hallucineert JSON), dan ziet de assessor de raw exception in de chat |
| Assessment-risico | Middel — schending van "geen stack traces" regel |
| Failure-mode | Crash met partial output |
| Aanbeveling | Sanitize error-detail (whitelist categorieën zoals `"llm_unavailable"`, `"timeout"`, `"validation_failed"`); log volledige exception met `request_id`; emit nette refuse-text als token-stream |

## 1.2 CRAG state machine — `demo/app/pipeline/crag.py`

| Aspect | Detail |
|---|---|
| Failure mode | `classify_query` faalt → `state.query_type = None` → `retrieve` krijgt `None` → BM25/kNN body shape OK maar daarna kan generator/grader stuk |
| Huidige gedrag | Geen try/except rond elke node ([crag.py:95-261](demo/app/pipeline/crag.py)). Eén failure cascade-stopt de hele pipeline. |
| Gebruikersimpact | 500 voor `/v1/query`; voor `/v1/chat` afhankelijk van waar in stream |
| Demo-risico | Middel |
| Assessment-risico | Hoog — "self-healing pipeline" claim wordt gebroken zodra één node faalt |
| Failure-mode | Crash |
| Aanbeveling | Per-node try/except + fail-closed naar refuse bij elke onverwerkte exception. Behoud refuse-paden als enige eindpunt |

## 1.3 Retrieval — `demo/app/pipeline/retriever.py`

| Aspect | Detail |
|---|---|
| Failure mode | OpenSearch onbereikbaar mid-query, embedding faalt, HyDE LLM-call faalt, sub-query parallel-retrieve gooit op één query |
| Huidige gedrag | Geen try/except op [retriever.py:152-153](demo/app/pipeline/retriever.py#L152) (`os_client.search` synchroon). HyDE failure → log warn, valt terug op raw query — fail-soft, goed. Sub-query `asyncio.gather` zonder `return_exceptions=True` → één failure cancelt allemaal. |
| Gebruikersimpact | OpenSearch-fail → user krijgt 500, geen refuse |
| Demo-risico | Hoog als OpenSearch yellow → red gaat |
| Assessment-risico | Middel |
| Failure-mode | Crash op OS-fail; fail-soft op HyDE; cascade-fail op sub-query |
| Aanbeveling | Wrap OS calls met retry+timeout; `asyncio.gather(..., return_exceptions=True)` voor sub-queries; bij totale OS-fail → refuse met "zoek-systeem tijdelijk onbereikbaar" |

## 1.4 Reranking — `demo/app/pipeline/reranker.py`

| Aspect | Detail |
|---|---|
| Failure mode | LLM JSON-mode geeft niet-parseable output, scores ontbreken, candidates list raakt corrupt |
| Huidige gedrag | [retriever.py:166-173](demo/app/pipeline/retriever.py#L166) heeft `try/except`, fallback naar RRF. Goed. |
| Gebruikersimpact | Geen — silent fallback |
| Demo-risico | Laag — featuere is `enable_llm_rerank=False` per default |
| Assessment-risico | Laag |
| Failure-mode | Fail-soft (correct) |
| Aanbeveling | Houden zoals het is; voeg trace-event toe `{node:"rerank_fallback"}` zodat het zichtbaar is in pipeline-trace |

## 1.5 Grading — `demo/app/pipeline/grader.py`

| Aspect | Detail |
|---|---|
| Failure mode | LLM JSON-mode gives `{}`/malformed; per-chunk score buiten 0-1; missing keys |
| Huidige gedrag | Heeft try/except [grader.py:46,59](demo/app/pipeline/grader.py#L46). Bij failure: alle chunks default RELEVANT → **fail-OPEN** ⚠⚠⚠ |
| Gebruikersimpact | LLM hallucineert door zonder grading-gate |
| Demo-risico | Hoog — de assessor zijn #1 anti-hallucinatie claim wordt geschonden |
| Assessment-risico | **Kritiek** — zero-hallucination tolerance is broken |
| Failure-mode | **Fail-OPEN — moet fail-CLOSED worden** |
| Aanbeveling | Bij grader-failure: default verdict = `IRRELEVANT` (niet RELEVANT) → refuse-pad. Liever zwijgen dan fout antwoorden — staat letterlijk in plan. |

## 1.6 Generation — `demo/app/pipeline/generator.py`

| Aspect | Detail |
|---|---|
| Failure mode | LLM produceert lege string, citaties zonder `[Source:..]` markers, max_tokens cap raakt mid-zin, het hallucineert ondanks system prompt |
| Huidige gedrag | [chat.py:220-223](demo/app/routers/chat.py#L220) checkt empty string → emit error. Citation-validation pad (regel 244+) is solide. |
| Gebruikersimpact | Bij empty-string: error-event |
| Demo-risico | Middel — koud Gemma kan af-en-toe lege string geven |
| Assessment-risico | Middel |
| Failure-mode | Fail-closed (correct) |
| Aanbeveling | Voeg een retry toe (1×) bij empty-string vóór refuse; emit `{node:"generate_retry"}` in trace |

## 1.7 Citation validation — `demo/app/pipeline/validator.py` + chat.py:227-244

| Aspect | Detail |
|---|---|
| Failure mode | Generator citeert `chunk_id` die niet in `graded` zit (hallucination), regex matcht niet, `cited_ids` is leeg |
| Huidige gedrag | [chat.py:227-243](demo/app/routers/chat.py#L227) heeft een **soft-recovery**: als geen citation gevonden, valt terug op `graded[:2]` chunk_ids. Dit is fail-OPEN — gebruiker krijgt antwoord zonder geverifieerde citations. |
| Gebruikersimpact | Antwoord met "afgeleide" citations |
| Demo-risico | de assessor kan ondervragen waarom een citation in het antwoord niet expliciet voorkomt |
| Assessment-risico | **Kritiek** — schending zero-hallucination tolerance |
| Failure-mode | **Fail-OPEN — moet fail-CLOSED worden** voor de strict path |
| Aanbeveling | Maak validator strict: bij geen citation → REFUSE met `INVALID_CITATIONS`-grade. Verwijder de `cited_ids = [c["chunk_id"] for c in graded[:2]]` fallback (chat.py:243) |

## 1.8 Semantic cache — `demo/app/pipeline/cache.py`

| Aspect | Detail |
|---|---|
| Failure mode | Vector-dim mismatch, Redis pipeline failure, JSON deserialize fail, embedding-on-store fails |
| Huidige gedrag | `_cosine` returns 0.0 op mismatch ([cache.py:47-54](demo/app/pipeline/cache.py#L47)), `_deserialize` returns `{}` op fail. Cross-tier check: in `check_cache_semantic` itereert per `accessible_tier` — corrrect. |
| Gebruikersimpact | Stille MISS i.p.v. HIT, gebruiker krijgt langere response |
| Demo-risico | Laag |
| Assessment-risico | Laag — never weaken RBAC; cross-tier integrity is OK |
| Failure-mode | Fail-closed (cache MISS) |
| Aanbeveling | Log dim-mismatch met `log.warn("cache_dim_mismatch", ...)`; emit cache-miss-reason in trace event zodat dit zichtbaar is voor reliability-bewijs |

## 1.9 Conversation memory — `demo/app/pipeline/memory.py`

| Aspect | Detail |
|---|---|
| Failure mode | Redis down mid-session, JSON deserialize fails, follow-up rewrite LLM fails |
| Huidige gedrag | [memory.py:34,42](demo/app/pipeline/memory.py#L34) — `redis_client.pipeline()` en `lrange` zonder try/except. Crash op redis-down. |
| Gebruikersimpact | Chat geeft 500 zodra Redis hiccup |
| Demo-risico | Middel |
| Assessment-risico | Laag (memory is een nice-to-have, geen critical RAG-feature) |
| Failure-mode | Crash |
| Aanbeveling | Wrap in try/except, log warn, return empty-history bij fail. Memory is per definitie best-effort — geen single point of failure rechtvaardigen voor een feature die bij eerste turn altijd "geen geschiedenis" is |

## 1.10 Document ingestion — `demo/app/ingestion/pipeline.py`

| Aspect | Detail |
|---|---|
| Failure mode | Chunk N van M faalt op enrich/embed/index; chunks 1..N-1 zijn al geïndexeerd, N+1..M nooit. Geen rollback. Same content uploaded 2× → chunk_ids deterministisch dus overwrite (idempotent), maar als doc-titel anders is → twee verschillende doc_ids met dezelfde inhoud. |
| Huidige gedrag | [ingestion/pipeline.py:176-216](demo/app/ingestion/pipeline.py#L176) — geen try/except rond enrich/embed/index. Single failure → loop break, partial state. Outer wrapper [ingest.py:82-89](demo/app/routers/ingest.py#L82) catcht exception en emit `{event:"error"}`. |
| Gebruikersimpact | UI toast "ingest fout"; deel chunks zichtbaar in corpus, deel niet. Geen way om te verifiëren |
| Demo-risico | Hoog — als één chunk in een 30-chunk PDF failt, breekt de "live ingestion"-act mid-demo |
| Assessment-risico | Middel — ingestion-pipeline is Module 1 van assessment |
| Failure-mode | Crash met partial state |
| Aanbeveling | Per-chunk try/except met `chunk_failed` event; doorgaan met rest; emit `complete` met `{succeeded: N, failed: M, skipped: K}`. Plus: content-hash check pre-index voor dedup detection (warning, niet blocker) |

## 1.11 PDF parsing — `demo/app/routers/ingest.py:33-45`

| Aspect | Detail |
|---|---|
| Failure mode | Encrypted PDF, scan-only image PDF (geen tekst), corrupted bytes, page-extract throws |
| Huidige gedrag | Per-page try/except in `_extract_text_from_pdf` — log warn en continue. Top-level try voor heel parse, geeft `{"error":"PDF-extractie mislukt"}` |
| Gebruikersimpact | Bij scan-PDF: 0 tekst → 1 lege chunk → ingest "succeeds" met onbruikbare content |
| Demo-risico | Middel |
| Assessment-risico | Laag |
| Failure-mode | Fail-soft maar onzichtbaar |
| Aanbeveling | Detecteer "extracted text < 100 chars" → return early met `{error: "no_text_extracted", hint: "Probeer OCR of upload een text-PDF"}`. Block scan-PDFs expliciet ipv silent garbage |

## 1.12 Chunking — `demo/app/ingestion/structural_chunker.py` + `semantic_chunker.py`

| Aspect | Detail |
|---|---|
| Failure mode | Regex matcht niets → 0 boundaries; semantic chunker LLM gives invalid offsets; offsets buiten doc-length |
| Huidige gedrag | [pipeline.py:123-127](demo/app/ingestion/pipeline.py#L123) heeft fallback naar `_fallback_one_boundary`. |
| Gebruikersimpact | Document ingest met 1 monolithische chunk (slecht voor retrieval) |
| Demo-risico | Laag |
| Assessment-risico | Laag |
| Failure-mode | Fail-soft (correct, maar suboptimal) |
| Aanbeveling | Bij `boundaries == 0` ná beide chunkers: voeg een 3e fallback toe — recursive char splitter (800 chars + 120 overlap). Markeer met `chunker_choice.path="recursive"` zodat het in trace zichtbaar is |

## 1.13 Metadata enrichment — `demo/app/ingestion/metadata_enricher.py`

| Aspect | Detail |
|---|---|
| Failure mode | LLM JSON-mode broken, returns wrong schema, returns nothing |
| Huidige gedrag | Try/except in `enrich()` — log warn, return `{}`. Goed. |
| Gebruikersimpact | Chunks worden geïndexeerd zonder topic/entities/summary — search blijft werken (BM25 + kNN op chunk_text) |
| Demo-risico | Laag |
| Assessment-risico | Laag |
| Failure-mode | Fail-soft (correct) |
| Aanbeveling | Houden. Voeg metric toe: `enrichment_success_rate` per ingest-run, log bij <80% success rate |

## 1.14 Embedding generation — `demo/app/pipeline/embedder.py`

| Aspect | Detail |
|---|---|
| Failure mode | sentence-transformers crashes (OOM, model not loaded), default executor thread-pool starvation onder concurrent load, race-condition op eerste `_load()` |
| Huidige gedrag | [embedder.py:22-32](demo/app/pipeline/embedder.py#L22) — global `_model` zonder lock. Eerste concurrent call kan dubbel laden. [Line 53](demo/app/pipeline/embedder.py#L53): default executor — onder concurrent ingest blokkeert het hele async pool |
| Gebruikersimpact | Onder concurrent load: 5-30s vertraging, soms timeout |
| Demo-risico | Middel — als demo niet pre-warmed, eerste call duurt 30+ sec |
| Assessment-risico | Middel |
| Failure-mode | Slow + soms crash |
| Aanbeveling | (1) `asyncio.Lock` rond eerste-load; (2) dedicated `concurrent.futures.ThreadPoolExecutor(max_workers=2)` voor embedder; (3) startup-warmup verifieer 1 dummy-embed |

## 1.15 OpenSearch dependency — `demo/app/opensearch/`

| Aspect | Detail |
|---|---|
| Failure mode | Cluster red, network partition, index missing, mapping mismatch (dim 384 vs 1024) |
| Huidige gedrag | [main.py:38](demo/app/main.py#L38) `setup_opensearch()` crash → lifespan crash → API starts NIET. Goed voor consistency, slecht voor demo: één Docker hiccup = handmatige restart. |
| Gebruikersimpact | API onbereikbaar |
| Demo-risico | Middel |
| Assessment-risico | Laag |
| Failure-mode | Crash bij startup, fail-closed runtime |
| Aanbeveling | Retry-with-backoff op startup-setup (5 attempts, 5s backoff). Runtime: per-call timeout (3s search, 10s index), expliciet 503 → refuse mapping |

## 1.16 Redis dependency — `demo/app/main.py:42-47`

| Aspect | Detail |
|---|---|
| Failure mode | Redis down, OOM, network partition, `decode_responses` mismatch op binary keys |
| Huidige gedrag | [main.py:47](demo/app/main.py#L47) `redis.ping()` crash → lifespan crash. Runtime: cache.py / memory.py / audit.py geen try/except → 500 op elke call. |
| Gebruikersimpact | API onbereikbaar bij startup; 500 errors runtime |
| Demo-risico | Middel |
| Assessment-risico | Laag (cache + audit zijn nice-to-haves voor de assessor) |
| Failure-mode | Crash |
| Aanbeveling | Startup-retry (3x). Runtime: wrap alle redis-calls in try/except, fail-soft (cache miss = no-op, audit fail = log warn, memory fail = empty history) |

## 1.17 Local Model Runner — `demo/app/pipeline/llm.py`

| Aspect | Detail |
|---|---|
| Failure mode | Model Runner crashed, model not pulled, 5-min timeout exceed, response truncated, JSON-mode unsupported |
| Huidige gedrag | M7 circuit-breaker is in. [llm.py:34](demo/app/pipeline/llm.py#L34) timeout=300s. JSON-mode fallback to plain ([llm.py:117-127](demo/app/pipeline/llm.py#L117)). Goed. |
| Gebruikersimpact | Bij open breaker → graceful refuse. Bij timeout → 500. |
| Demo-risico | Middel |
| Assessment-risico | Laag |
| Failure-mode | Mostly fail-closed. Timeout = crash. |
| Aanbeveling | Per-call timeout (15s voor classify/grade, 60s voor generate). Retry 1× op `httpx.ReadTimeout` voordat breaker on_failure. Distinct timeout from breaker-trip |

## 1.18 RBAC en tier filtering — `demo/app/security/rbac.py`, `demo/app/pipeline/cache.py`

| Aspect | Detail |
|---|---|
| Failure mode | `build_rbac_filter()` produceert lege filter (alle tiers toegankelijk!), tier-string corrupt, cache cross-tier leak |
| Huidige gedrag | RBAC filter is statisch (`TIER_ACCESS` mapping). Cache itereert `accessible_tiers` lijst. `build_rbac_filter` heeft geen safeguard tegen ongeldige tier (returns broad filter?) |
| Gebruikersimpact | Mogelijk: helpdesk ziet FIOD-content |
| Demo-risico | **Catastrofaal** als cross-tier leak demonstrabel is |
| Assessment-risico | **Kritiek** |
| Failure-mode | Need to verify — moet **fail-CLOSED** zijn (default = PUBLIC alleen) |
| Aanbeveling | Audit `build_rbac_filter` — bij ongeldige tier: raise / return "PUBLIC-only". Add unit-test: `build_rbac_filter("INVALID_TIER")` → must restrict to PUBLIC. Add integration-test: helpdesk-tier query nooit FIOD-chunk in response |

## 1.19 Frontend error states — `demo/app/static/app.js`

| Aspect | Detail |
|---|---|
| Failure mode | SSE break mid-stream, fetch returns 500, partial response, citations not loaded |
| Huidige gedrag | [app.js:691](demo/app/static/app.js#L691) `case "error"` — toon raw `data.detail` als toast + inline error. Geen retry-knop. Stream halt-state. |
| Gebruikersimpact | Toast met (mogelijk) tech-jargon; gebruiker moet zelf opnieuw vragen |
| Demo-risico | Middel — de assessor ziet rauwe error, vraagt waarom |
| Assessment-risico | Middel |
| Failure-mode | Fail-soft maar lelijk |
| Aanbeveling | Categoriseer error events server-side; frontend mapt categorie → vriendelijke NL-tekst + retry-button. Geen raw error-text naar user. Inline retry zonder pagina-refresh |

## 1.20 Health checks en startup readiness — `demo/app/routers/health.py`

| Aspect | Detail |
|---|---|
| Failure mode | `/health` zegt "ready" terwijl LLM unreachable (zie main.py:32-33 — log warn maar `warmup_complete = True`) |
| Huidige gedrag | [main.py:32-33](demo/app/main.py#L32) → warning, doorgaan. Lifespan zet line 50 `warmup_complete = True` ongeacht LLM-status. **Liegt.** |
| Gebruikersimpact | UI splash gaat weg, eerste query crasht |
| Demo-risico | Hoog — tijdens demo geeft API "ready" maar eerste query 500 |
| Assessment-risico | Middel |
| Failure-mode | Fail-OPEN (zegt OK terwijl niet) |
| Aanbeveling | Dual endpoints: `/health` (process alive) + `/readyz` (alle deps ping). Splash wacht op `/readyz`. LLM ping fail bij startup → `warmup_complete = False` blijft tot LLM reageert (background-poll iedere 5s) |

---

# Deel 2 — Implementatieplan

5 sprints. Elke taak heeft **wat / waarom / files / tijd / acceptance**.

## Sprint S1 — Stop the bleeding (5-7 uur)

Demo-blockers; doe deze eerst.

### S1.1 Fail-CLOSED grader bij failure (1u) ⚠ KRITIEK
**Wat:** [grader.py:46-59](demo/app/pipeline/grader.py#L46) — bij JSON-parse-failure, default verdict naar IRRELEVANT (niet RELEVANT).
**Waarom:** Anders bypass van zero-hallucination-gate.
**Acceptance:** Force-broken grader-prompt → grading_result = "IRRELEVANT" → refuse-pad.

### S1.2 Fail-CLOSED citation validator (1u) ⚠ KRITIEK
**Wat:** Verwijder fallback `cited_ids = [c["chunk_id"] for c in graded[:2]]` op [chat.py:243](demo/app/routers/chat.py#L243). Als geen citations gevonden → `validation["valid"] = False` → refuse via INVALID_CITATIONS pad.
**Waarom:** Anders krijgt user antwoord met "verzonnen" citations.
**Acceptance:** LLM-output zonder `[Source:...]` → REFUSE, niet RESPONSE.

### S1.3 Per-node try/except in CRAG en chat-stream (1.5u)
**Wat:** Wrap elke node-call (classify, retrieve, grade, generate, validate) in try/except. Bij failure → emit `{node, result:"FAILED", detail:safe_msg, duration_ms}` → refuse.
**Files:** [crag.py:77-260](demo/app/pipeline/crag.py), [chat.py:74-300](demo/app/routers/chat.py).
**Acceptance:** Force-throw in classifier → user krijgt nette refuse, geen 500.

### S1.4 Sanitize error-events naar frontend (45m)
**Wat:** Centraliseer error-categorieën: `LLM_UNAVAILABLE`, `TIMEOUT`, `VALIDATION_FAILED`, `INFRA_ERROR`, `INTERNAL`. Geen raw `str(e)` naar client. Server logt full exception met request_id.
**Files:** chat.py, ingest.py, eval_dashboard.py.
**Acceptance:** Force-exception → SSE error-event = `{"category":"INTERNAL", "message":"<NL friendly>", "request_id":"abc123"}`.

### S1.5 `/readyz` endpoint dat LLM verifieert (1u)
**Wat:** Splits `/health` (proces alive) van `/readyz` (alle deps OK). Lifespan zet `warmup_complete=True` alleen als LLM-ping slaagt. Frontend splash polled `/readyz`.
**Files:** [main.py:30-52](demo/app/main.py#L30), [health.py](demo/app/routers/health.py), [app.js splash logic](demo/app/static/app.js).
**Acceptance:** LLM uitschakelen → `/readyz` 503 + reason; splash blijft hangen tot LLM up.

### S1.6 Sanity-check `build_rbac_filter` op ongeldige tier (45m)
**Wat:** [security/rbac.py](demo/app/security/rbac.py) — bij onbekende tier-string return PUBLIC-only filter (fail-CLOSED). Plus: integration-test "helpdesk + FIOD-query → 0 FIOD-chunks in response".
**Acceptance:** Unit + integration-test groen; documenteer in slide 4.

## Sprint S2 — Validation, retry, timeout (5-7 uur)

### S2.1 Pydantic-validation op alle request-bodies (1u)
**Wat:** Audit alle `BaseModel` request-types. Voeg `Field(min_length, max_length, regex)` constraints. SecurityTier moet enum-strict zijn (FastAPI doet dit al, verifieer).
**Files:** chat.py, query.py, ingest.py.
**Acceptance:** `curl ... -d '{"query":""}' ` → 422 met duidelijke validation error.

### S2.2 Per-call timeouts gedifferentieerd (1u)
**Wat:** Vervang globale `llm_timeout_s=300` door per-call:
- classify/grade/decompose/hyde: 15s
- generate (streaming): 60s totaal, 5s tussen tokens (idle timeout)
- enrich: 30s
**Files:** [llm.py:34](demo/app/pipeline/llm.py#L34), [config.py](demo/app/config.py).
**Acceptance:** Force long-running call → timeout in juiste window, breaker.on_failure() called.

### S2.3 Retry-with-backoff op transient failures (1.5u)
**Wat:** `tenacity`-style retry op:
- OpenSearch ConnectionError: 3× met 0.5s/1s/2s backoff
- LLM ReadTimeout: 1× retry zonder backoff (snelle pre-breaker chance)
- Redis ConnectionError: 2× met 0.5s/1s
**Files:** llm.py, retriever.py, cache.py, memory.py, audit.py.
**Acceptance:** kill-redis voor 1s → request slaagt na retry.

### S2.4 `asyncio.gather(return_exceptions=True)` voor sub-queries (30m)
**Wat:** [chat.py decompose path](demo/app/routers/chat.py) — sub-query parallel-retrieve. Bij één failure: log warn, gebruik andere sub-query resultaten + 1 failure-trace event. Bij ALLE failed: refuse.
**Acceptance:** Force exception in 1 van 3 sub-queries → resultaten van 2 andere gemerged, trace toont `decompose_partial`.

### S2.5 Embedder asyncio-Lock + dedicated thread pool (1u)
**Wat:** [embedder.py:22-32](demo/app/pipeline/embedder.py#L22) — `asyncio.Lock` rond `_load()`. [embedder.py:53](demo/app/pipeline/embedder.py#L53) — dedicated `ThreadPoolExecutor(max_workers=2)` (configurable via settings) i.p.v. default executor.
**Acceptance:** Stress-test 20 parallel queries → geen dubbel-load log; main event loop niet starved (verifieer met /health response time tijdens stress).

### S2.6 LLM generation retry op empty-string (30m)
**Wat:** [chat.py:220](demo/app/routers/chat.py#L220) — bij `not full_text.strip()` 1× retry vóór error. Emit `{node:"generate_retry"}` in trace.
**Acceptance:** Force empty-LLM-response → retry → success of refuse.

## Sprint S3 — Ingestion robustness + idempotency (4-5 uur)

### S3.1 Per-chunk try/except in ingest pipeline (1.5u) ⚠ DEMO-PROOF
**Wat:** [ingestion/pipeline.py:131-216](demo/app/ingestion/pipeline.py#L131) — wrap enrich, embed, index in afzonderlijke try/except. Bij failure: emit `{type:"chunk_failed", chunk_id, stage, reason}`; doorgaan met volgende chunk. Final event: `{type:"complete", succeeded, failed, total_ms}`.
**Acceptance:** Force-fail op chunk 5 van 30 → 29 chunks geïndexeerd, 1 failed; UI toont gele waarschuwing.

### S3.2 Content-hash dedup op /v1/ingest (1u)
**Wat:** Voor ingest start: SHA256 van text content. Check OpenSearch op `content_hash:<hash>` term query. Als hit: emit `{type:"duplicate", existing_doc_id, action:"skipped"}` en stop. Optioneel: `?force=true` query-param om opnieuw te indexen.
**Files:** [ingest.py:48-91](demo/app/routers/ingest.py#L48), [ingestion/pipeline.py](demo/app/ingestion/pipeline.py), opensearch index mapping (add content_hash field).
**Acceptance:** Upload zelfde PDF 2× → tweede upload gaf "duplicate, skipped".

### S3.3 PDF "leegheid"-check (45m)
**Wat:** [ingest.py:33-45](demo/app/routers/ingest.py#L33) — na `_extract_text_from_pdf`, als `len(text.strip()) < 100`: return early met `{error:"no_text_extracted", hint:"Probeer text-PDF; deze lijkt scan-only"}`. UI toont nette boodschap.
**Acceptance:** Upload scan-PDF → vroege fail, geen orphan-chunks.

### S3.4 Recursive char-splitter fallback (45m)
**Wat:** [ingestion/pipeline.py:123](demo/app/ingestion/pipeline.py#L123) — voeg derde fallback toe (na semantic) met 800-char windows + 120-char overlap. Markeer `chunker_choice.path="recursive"`.
**Acceptance:** Upload doc zonder structuur → semantic chunker fails → recursive splitter succesvol.

### S3.5 Ingestion concurrency-lock per doc_id (30m)
**Wat:** Redis-lock `ingest:{doc_id}` met TTL 5min. Voorkomt parallelle ingests van zelfde doc.
**Acceptance:** Twee parallel `curl /v1/ingest` van zelfde file → tweede krijgt 409 met "ingest already in progress".

## Sprint S4 — Frontend state polish (3-4 uur)

### S4.1 Categorized error UI (1.5u)
**Wat:** [app.js handleChatEvent error case](demo/app/static/app.js#L691) — ontvang `{category, message, request_id}`. Map per category naar:
- `LLM_UNAVAILABLE`: amber bubble, "Inferentie tijdelijk weg, probeer over 30s"
- `TIMEOUT`: amber bubble, "Verwerking duurde te lang", retry-knop
- `VALIDATION_FAILED`: rode bubble, "Vraag is leeg of te lang"
- `INFRA_ERROR`: amber bubble, "Iets ging mis (req-id: abc123)", retry
- `INTERNAL`: amber, generic
Inline retry-button vervangt "stuur nieuwe vraag".
**Acceptance:** Force elk type error → bijbehorende bubble + button.

### S4.2 Loading skeleton voor langzame eerste call (45m)
**Wat:** Bestaande progress-strip blijft, voeg na 8s een explicit "model warmt op (eerste call kan ~30s duren — daarna < 2s via cache)" toe. Gebruiker weet wat te verwachten.
**Acceptance:** Cold start → na 8s verschijnt warmup-tekst.

### S4.3 SSE reconnect-on-disconnect (1u)
**Wat:** Bij mid-stream disconnect detecteren in `streamChat` (reader.read returns done before "done" event); markeer turn als incomplete; toon "Verbinding verbroken — opnieuw proberen?" knop. Vermijd zelfde turn dubbel; bij retry, nieuwe session-context preserve.
**Acceptance:** kill-api mid-stream → frontend toont disconnect-banner met retry, geen frozen UI.

### S4.4 Status-banner in topbar bij degraded mode (45m)
**Wat:** Polling `/readyz` elke 30s in achtergrond. Bij 503: toon banner bovenaan "Inferentie-systeem opnieuw bezig op te starten — sommige features tijdelijk uit". Verwijder banner bij `/readyz` 200.
**Acceptance:** kill-Model-Runner → banner verschijnt binnen 30s; restart → banner weg.

## Sprint S5 — Observability + reliability bewijs (3-5 uur)

### S5.1 Request-ID propagation (1u)
**Wat:** FastAPI middleware genereert `X-Request-ID` per request (UUID). Logt in elke structlog-call. Emit in elke SSE error-event. Frontend toont laatste request_id bij error.
**Files:** main.py (middleware), alle routers.
**Acceptance:** Trigger error → request_id in UI matcht regex in `docker logs` output.

### S5.2 Reliability-metrics op Kwaliteit-tab (1.5u)
**Wat:** Nieuwe Redis-counters:
- `metric:llm_calls_total`, `metric:llm_calls_failed`
- `metric:cache_hits`, `metric:cache_misses`
- `metric:refuse_total` per reason (`IRRELEVANT`, `INVALID_CITATIONS`, `BREAKER_OPEN`, `INFRA`)
- `metric:ingest_chunks_succeeded`, `metric:ingest_chunks_failed`

Endpoint `/v1/metrics/summary` → JSON met counters + computed rates. Show in 3 cards op Kwaliteit-tab onder de eval-metrics.
**Files:** new `app/metrics.py` helper, eval_dashboard.py, app.js Kwaliteit-section, index.html.
**Acceptance:** 10 queries → counters increment correct; refuse-rate zichtbaar in UI.

### S5.3 Trace-events voor reliability bewijs (1u)
**Wat:** Voeg trace-events toe die fail-paths zichtbaar maken:
- `{node:"grader_fallback", result:"IRRELEVANT", detail:"json_parse_failed"}`
- `{node:"retrieve_retry", result:"recovered", detail:"OS reconnect after 1s"}`
- `{node:"breaker_state", result:"OPEN→HALF_OPEN"}`

de assessor ziet in CRAG-pagina expliciet waar reliability-mechanismen vuren.
**Acceptance:** Force breaker-open + recovery → CRAG-diagram toont breaker-state-overgang.

### S5.4 Chaos-test mode (1u)
**Wat:** Nieuwe endpoint `POST /v1/admin/chaos/{action}` (auth-loos voor demo, prod kan disabled): `force_breaker_open`, `kill_redis_briefly`, `slow_down_llm`. Bedoeld voor live-demo: de assessor ziet de fail-safety in actie.
**Files:** new `app/routers/chaos.py`, breaker.py.
**Acceptance:** `curl POST /v1/admin/chaos/force_breaker_open` → breaker is OPEN → volgende chat-call krijgt graceful refuse zichtbaar in trace.

### S5.5 OpenTelemetry tracing optie (skip indien tijd nip) (1.5u)
**Wat:** OTEL-trace via `opentelemetry-instrumentation-fastapi`; Jaeger als sidecar in compose. Span per CRAG-node met latency + status. Productie-signaal sterker dan logs alleen.
**Acceptance:** Open Jaeger UI op `localhost:16686` → trace per chat-request met alle spans zichtbaar.

---

# Deel 3 — Demo-voorbeelden voor reliability-bewijs

Tijdens de live demo expliciet 3 reliability-momenten tonen:

## Demo-moment 1 — Hallucination-catch (Act 6 in DEMO_SCRIPT)
**Trigger:** "Who built the Eiffel Tower?"
**Wat de assessor ziet:** Pipeline trace toont `retrieve → 0 chunks → grade_context → IRRELEVANT → refuse`. Refuse-bubble heeft amber border + uitleg. *"Dit is fail-closed bij geen geverifieerde context."*

## Demo-moment 2 — Force-breaker via chaos endpoint
**Trigger:** Voorafgaand aan demo of live: `curl -X POST localhost:8000/v1/admin/chaos/force_breaker_open` → daarna een normale chat-vraag.
**Wat de assessor ziet:** Trace toont `breaker_state: OPEN`, geen LLM-call, refuse-bubble *"inferentie tijdelijk overbelast"*. *"Productie-pattern. 30 seconden later automatisch herstel."*

## Demo-moment 3 — Live-ingestion partial failure (force een chunk-fail)
**Trigger:** Upload een PDF; tijdens stream toon dat één chunk faalt (use een chaos-flag in metadata enricher).
**Wat de assessor ziet:** Stream toont `chunk_failed` event voor 1 chunk, maar gaat door met de andere 29; final event `{succeeded:29, failed:1}`. *"Geen rollback, geen orphans, partial-success-rapportage."*

## Demo-moment 4 — Audit-trail toont reliability events
**Trigger:** Toon Operations → Toegang met de audit-tabel.
**Wat de assessor ziet:** Naast normale queries staan rijen met grade `BREAKER_OPEN` en `INFRA` — *"Elke uitval is gelogd, niet stil weggevangen. Compliance-vereiste."*

## Demo-moment 5 — Kwaliteit-tab metrics
**Trigger:** Operations → Kwaliteit, scrol naar reliability-cards.
**Wat de assessor ziet:**
- LLM call success rate: 99.4%
- Cache hit ratio: 23%
- Refuse-rate by reason: IRRELEVANT 3.1%, BREAKER 0.2%, INFRA 0.1%
*"In productie zou hier een SLO-tracker zitten. Voor de demo: dit zijn de afgelopen 100 queries."*

---

# Deel 4 — Specifieke files/modules/endpoints die wijzigen

| Status | Pad | Reden | Sprint |
|---|---|---|---|
| EDIT | [demo/app/main.py](demo/app/main.py) | LLM ping verifieer voor warmup_complete; middleware request-id | S1.5, S5.1 |
| EDIT | [demo/app/pipeline/grader.py](demo/app/pipeline/grader.py) | Fail-CLOSED bij parse-error | S1.1 |
| EDIT | [demo/app/pipeline/crag.py](demo/app/pipeline/crag.py) | Per-node try/except + safe refuse | S1.3 |
| EDIT | [demo/app/routers/chat.py](demo/app/routers/chat.py) | Strict citation validator, sanitize errors, retry op empty | S1.2, S1.4, S2.4, S2.6 |
| EDIT | [demo/app/pipeline/llm.py](demo/app/pipeline/llm.py) | Per-call timeouts, retry-on-timeout | S2.2, S2.3 |
| EDIT | [demo/app/pipeline/embedder.py](demo/app/pipeline/embedder.py) | asyncio.Lock + dedicated executor | S2.5 |
| EDIT | [demo/app/pipeline/cache.py](demo/app/pipeline/cache.py) | Try/except wraps + retry | S2.3 |
| EDIT | [demo/app/pipeline/memory.py](demo/app/pipeline/memory.py) | Try/except + fail-soft | S2.3 |
| EDIT | [demo/app/audit.py](demo/app/audit.py) | Try/except + safe-fail | S2.3 |
| EDIT | [demo/app/pipeline/retriever.py](demo/app/pipeline/retriever.py) | OS retry, gather return_exceptions | S2.3, S2.4 |
| EDIT | [demo/app/ingestion/pipeline.py](demo/app/ingestion/pipeline.py) | Per-chunk try/except, content-hash dedup, recursive fallback | S3.1, S3.2, S3.4 |
| EDIT | [demo/app/routers/ingest.py](demo/app/routers/ingest.py) | PDF-leegheid check, content-hash, lock | S3.2, S3.3, S3.5 |
| EDIT | [demo/app/security/rbac.py](demo/app/security/rbac.py) | Fail-closed op ongeldige tier | S1.6 |
| EDIT | [demo/app/routers/health.py](demo/app/routers/health.py) | /readyz endpoint | S1.5 |
| EDIT | [demo/app/static/app.js](demo/app/static/app.js) | Categorized error UI, retry-button, reconnect, status banner | S4.1-S4.4 |
| EDIT | [demo/app/static/index.html](demo/app/static/index.html) | Reliability-cards op Kwaliteit-tab | S5.2 |
| NEW | demo/app/metrics.py | Reliability counters | S5.2 |
| NEW | demo/app/routers/chaos.py | Demo chaos endpoints | S5.4 |
| NEW | demo/app/routers/middleware.py | Request-ID middleware | S5.1 |
| EDIT | [demo/app/opensearch/setup.py](demo/app/opensearch/setup.py) | content_hash field in mapping | S3.2 |
| EDIT | [demo/requirements-demo.txt](demo/requirements-demo.txt) | tenacity (retry), opentelemetry (optional) | S2.3, S5.5 |
| EDIT | [demo/docker-compose.yml](demo/docker-compose.yml) | Jaeger sidecar (optional) | S5.5 |

---

# Deel 5 — Testing checklist

Per sprint, de tests die moeten draaien voor groen-licht.

## S1 — Stop the bleeding
- [ ] **S1.1**: kill grader-prompt → grading_result == "IRRELEVANT" (curl-test)
- [ ] **S1.2**: force LLM zonder citations → response.source == "refuse" + grading_result == "INVALID_CITATIONS"
- [ ] **S1.3**: monkey-patch classifier to throw → user krijgt refuse-bubble, geen 500
- [ ] **S1.4**: SSE error-event has `{category, message, request_id}` schema, geen `str(e)` payload
- [ ] **S1.5**: stop Model Runner → `/readyz` → HTTP 503 met reason. Frontend splash blijft hangen.
- [ ] **S1.6**: `build_rbac_filter("HACKER_TIER")` returns PUBLIC-only filter; integration: PUBLIC-user FIOD-query → 0 FIOD-chunks

## S2 — Validation, retry, timeout
- [ ] **S2.1**: empty query → 422 met expliciete validation-error
- [ ] **S2.2**: classify call vastlopen → 15s timeout → graceful refuse
- [ ] **S2.3**: kill Redis voor 1s → next request slaagt na retry-backoff
- [ ] **S2.4**: 1 sub-query gooit → andere 2 slaagt → response merge correct, trace toont `decompose_partial`
- [ ] **S2.5**: 20 parallel queries → geen `embedder_loaded` dubbel-log; `/health` response < 100ms
- [ ] **S2.6**: force-empty-LLM → retry → success OR refuse, geen error-event

## S3 — Ingestion robustness
- [ ] **S3.1**: ingest 30-chunk PDF, fail chunk 15 → 29 chunks indexed, 1 failed, complete-event toont counts
- [ ] **S3.2**: zelfde PDF 2× uploaden → tweede skip met `duplicate` event
- [ ] **S3.3**: scan-only PDF → vroege fail met `no_text_extracted`
- [ ] **S3.4**: doc zonder structuur en zonder semantic-cuts → recursive splitter, chunks geïndexeerd
- [ ] **S3.5**: parallel `/v1/ingest` zelfde file → tweede 409

## S4 — Frontend state polish
- [ ] **S4.1**: trigger elk error-categorie → bijbehorende bubble + retry-button
- [ ] **S4.2**: cold start → na 8s verschijnt warmup-uitleg
- [ ] **S4.3**: kill api mid-stream → reconnect-banner; Retry werkt
- [ ] **S4.4**: kill Model Runner → topbar-banner binnen 30s; restart → banner verdwijnt

## S5 — Observability
- [ ] **S5.1**: error → SSE-event en docker-logs hebben matching request_id
- [ ] **S5.2**: 10 queries → metrics-cards updaten; refuse-rate per reason zichtbaar
- [ ] **S5.3**: force breaker-open → trace toont `breaker_state` overgang
- [ ] **S5.4**: chaos endpoint trip breaker → demo-flow continues gracefully
- [ ] **S5.5**: Jaeger UI toont span-tree per request (optional)

## End-to-end demo-rehearsal smoke
- [ ] Volledige DEMO_SCRIPT (8 acts) uitvoeren met chaos endpoints geactiveerd op act 6 → alles werkt fail-safe
- [ ] Geen 500 of stack trace zichtbaar in browser-console of UI tijdens hele demo
- [ ] Audit-trail bevat alle queries inclusief refuse-paden met reason

---

# Deel 6 — Geprioriteerde roadmap

| Sprint | Tijd | Doel | Risico-reductie | Volgorde |
|---|---|---|---|---|
| **S1 Stop-the-bleeding** | 5-7u | Geen 500's, geen stack traces, fail-CLOSED grader/citations | **Catastrofaal → Laag** | DOEN EERST |
| **S2 Validation/retry/timeout** | 5-7u | Transient failures niet gebruiker-zichtbaar | Hoog → Middel | DAARNA |
| **S3 Ingestion robustness** | 4-5u | Live-ingestion act in demo niet brekend | Middel → Laag | KAN PARALLEL MET S2 |
| **S4 Frontend polish** | 3-4u | Error-state lijkt op feature, niet op bug | Middel → Laag | NA S1+S2 |
| **S5 Observability + bewijs** | 3-5u | Reliability is **zichtbaar** voor de assessor | N/A — assessment-impact | LAATSTE |

**Totaal: 20-28 uur. 1 werkweek voor 1 engineer.**

**Minimaal scenario** (5-7u): doe alleen S1. Daarna is het systeem demo-veilig op het kritieke vlak (geen hallucinations, geen stack traces, RBAC fail-closed). Niet productie-klaar maar wel assessment-veilig.

**Recommended** (12-15u): S1 + S2 + S3.1. Beste tijd/impact-ratio.

**Volledig** (20-28u): alle vijf sprints. Geeft demo + assessment + sterke productie-vibe.

---

# Deel 7 — Concrete Claude Code-tasks

Geformuleerd zoals de gebruiker een Claude Code-sessie zou starten:

```
TASK 1 — S1.1+S1.2+S1.6: fail-CLOSED critical paths
"Maak grader fail-closed bij JSON-parse-error (default IRRELEVANT). Verwijder citation-fallback in chat.py:243.
Audit build_rbac_filter — bij ongeldige tier → PUBLIC-only. Acceptance: drie unit-tests groen,
plus end-to-end: corrupt grader-prompt → refuse, geen RESPONSE."
```

```
TASK 2 — S1.3+S1.4: per-node try/except en safe error-events
"Wrap elke CRAG-node call in try/except met fallback naar refuse. Centraliseer error-event schema
{category, message, request_id}. Geen raw str(e) naar client. Acceptance: monkey-patch classify
to throw → user-bubble toont nette refuse, server-log heeft request_id matchbaar in browser."
```

```
TASK 3 — S1.5: /readyz endpoint
"Splits /health (process up) van /readyz (LLM + Redis + OpenSearch ping). Lifespan zet
warmup_complete=True alleen als LLM ping slaagt; background-poll iedere 5s als LLM nog niet up.
Frontend splash polled /readyz. Acceptance: stop Model Runner → /readyz 503; splash blijft hangen
tot Model Runner up komt."
```

```
TASK 4 — S2: timeouts, retry, concurrency
"Per-call timeouts via config.py (classify 15s, generate 60s, enrich 30s). Tenacity-retry op
OpenSearch ConnectionError + Redis ConnectionError. asyncio.gather(return_exceptions=True)
voor sub-query parallel. Embedder asyncio.Lock + dedicated ThreadPoolExecutor(max_workers=2).
Acceptance: zie testing checklist S2."
```

```
TASK 5 — S3: ingestion hardening
"Per-chunk try/except in ingestion/pipeline.py — emit chunk_failed event, doorgaan met rest,
final complete-event met counts. Content-hash dedup op /v1/ingest. PDF leegheid-check.
Recursive char-splitter als 3e fallback. Acceptance: zie checklist S3."
```

```
TASK 6 — S4: frontend error UI
"Categorized error-handling in app.js. Inline retry-button per error-bubble. Status-banner in
topbar polling /readyz. Reconnect-on-disconnect voor SSE. Acceptance: zie checklist S4."
```

```
TASK 7 — S5: observability en chaos-mode
"Request-ID middleware in main.py. Reliability-counters in app/metrics.py + UI cards op
Kwaliteit-tab. Chaos-endpoints in app/routers/chaos.py voor demo (force_breaker, kill_redis,
slow_llm). Acceptance: zie checklist S5."
```

---

# Deel 8 — Final pre-demo reliability checklist

Eén dag voor de demo:

## Functional verificatie
- [ ] `docker compose down -v && docker compose up -d --build` start vanaf nul
- [ ] `/readyz` 200 binnen 90s op fresh stack
- [ ] Pre-warm 4 demo-queries; alle 4 succeed met TTFT < 5s warm
- [ ] Run Ragas-eval (1-3 min); cijfers zichtbaar op Kwaliteit-tab
- [ ] Pre-ingest 12 documenten via preingest.sh; corpus heeft >100 chunks
- [ ] Tier-switch test: PUBLIC user FIOD-query → refuse; FIOD user zelfde query → response

## Reliability verificatie
- [ ] **Force breaker open** via chaos endpoint → next chat-call refuses gracefully met BREAKER_OPEN reden
- [ ] **Kill Redis voor 1s** via `docker compose pause redis && sleep 1 && unpause` → request slaagt na retry
- [ ] **Force grader corruption** (eenmalig override prompt) → response gaat naar refuse, niet naar generation
- [ ] **Force generator empty-string** → retry-pad fired, anders refuse
- [ ] **PDF scan upload** → vroege fail met `no_text_extracted` reden
- [ ] **Duplicate PDF upload** → `duplicate` skip event
- [ ] **Concurrent ingest** zelfde doc → 409 conflict
- [ ] **OS slow query** simulatie → time-out → graceful refuse, niet 500

## Observability verificatie
- [ ] Request-ID zichtbaar in elke error-bubble en docker-logs (matchbare UUID)
- [ ] Audit-trail laat queries van laatste 30 min zien met juiste tier en reden
- [ ] Reliability-counters update ná elk type query
- [ ] Trace-events in CRAG-pagina tonen alle reliability-momenten (`grader_fallback`, `breaker_state`)

## Demo-script verificatie
- [ ] DEMO_SCRIPT.md alle 8 acts → geen blokkers
- [ ] Backup-screencast opgenomen (`demo/recordings/`)
- [ ] Chaos-endpoints werken voor de "Demo-moment 2" force-breaker scene
- [ ] Slide 4 (Toegang) noemt expliciet fail-closed RBAC
- [ ] Slide 3 (CRAG) noemt expliciet fail-closed grader + breaker

## Hardware/omgeving
- [ ] WiFi uit; alle externe URLs in browser-tabs vooraf geladen
- [ ] Externe monitor mirrored
- [ ] Browser 100% zoom incognito
- [ ] Hard refresh `?v=16` actief
- [ ] Slack/Teams/IDE/heavy apps gesloten

---

# Slot

Deze plan haalt de inzending van *"werkt meestal"* naar *"faalt veilig"*. Het belangrijkste inzicht: **drie van de huidige fail-paths zijn fail-OPEN** (grader, citation-validator, /health-warmup-lie) — die schenden direct de assessor zijn expliciete eisen. Sprint S1 alleen al sluit die gaten en verlaagt het assessment-risico catastrofaal → laag.

De rest (S2-S5) is dieper productie-werk dat het verschil maakt tussen een "eerlijke prototype demo" en een "ja, deze persoon weet hoe je dit op productie zet". Voor een Lead AI Engineer-rol bij Belastingdienst is dat tweede precies wat de assessor wil zien.

---

*Geen code gewijzigd. Plan klaar voor uitvoering — geef groen licht voor sprint S1, of voor een aangepaste subset.*
