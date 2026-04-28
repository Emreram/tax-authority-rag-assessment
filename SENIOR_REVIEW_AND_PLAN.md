# Senior-Level Review & Improvement Plan

Datum: 2026-04-28 — peer-review op de huidige state na de UI-strip, slide-deck en commits.

> Oordeel staat in deze review — geen code wordt gewijzigd. De plan-sectie aan het eind beschrijft wat er moet gebeuren om de inzending van mid-niveau naar senior-niveau te tillen.

---

# Deel 1 — Diepe review

## 1.1 Architectuurniveau

### Wat zit er

| Onderdeel | File | Status |
|---|---|---|
| 9-state CRAG-machine | [demo/app/pipeline/crag.py](demo/app/pipeline/crag.py) (260 regels) | Imperatief, traceerbaar, refuse-paden expliciet |
| Hybride retrieval (BM25 + kNN + RRF) | [demo/app/pipeline/retriever.py](demo/app/pipeline/retriever.py) (174 regels) | RRF k=60, kNN met e5-small |
| LLM-as-reranker | [demo/app/pipeline/reranker.py](demo/app/pipeline/reranker.py) (63 regels) | JSON-mode call op zelfde Gemma |
| Grader | [demo/app/pipeline/grader.py](demo/app/pipeline/grader.py) (79 regels) | Batched JSON-call, drie verdicts |
| Semantic cache | [demo/app/pipeline/cache.py](demo/app/pipeline/cache.py) (196 regels) | Tier-gepartitioneerd, cosine ≥ 0.97 |
| Conversation memory | [demo/app/pipeline/memory.py](demo/app/pipeline/memory.py) (91 regels) | Redis rolling window |
| HyDE | [demo/app/pipeline/hyde.py](demo/app/pipeline/hyde.py) (33 regels) | Aanwezig, **niet zichtbaar firende in demo** |
| Live ingestion pipeline | [demo/app/ingestion/](demo/app/ingestion/) (520 regels) | Structurele + semantische chunker, AI-metadata-enricher |
| Pre-retrieval RBAC | OpenSearch `bool.filter` clause vóór scoring | Mathematisch defendable |
| 4-tier model | PUBLIC / INTERNAL / RESTRICTED / CLASSIFIED_FIOD | Monotone inclusie |
| Streaming chat (SSE) | [demo/app/routers/chat.py](demo/app/routers/chat.py) (317 regels) | Per-node trace events |
| Frontend split | Werkruimte (Gesprek, Documenten) + Operations (Ingestie, Retrieval, CRAG, Toegang, Kwaliteit) | Productgevoel ipv dev-tool |

**Verdict architectuur:** **senior-laag stevig.** De architectuur is op zichzelf assessment-waardig. Het probleem zit in de bewijsvoering en de finishing.

## 1.2 Productervaring (eindgebruiker)

### Wat werkt

- Gesprek met token-streaming, citations, parent-context badges, progress-strip
- Suggested prompts (5) als startpunt
- Document upload met live chunk-stream
- Tier-switch direct zichtbaar via role-buttons
- Hard-refuse op IRRELEVANT-paden i.p.v. hallucinatie

### Wat nog dev-smaak heeft

- **De Operations-tabs zijn nog steeds te zichtbaar in de productervaring** — een echte eindgebruiker (helpdesk-medewerker) zou ze niet eens moeten zien. Het split-pattern is goed, maar Operations zou achter een rol-check moeten zitten ("alleen Inspecteur en hoger").
- Geen gebruikersfeedback bij refuse: "Geen relevante context gevonden" ziet eruit als een fout, niet als een feature. Een betere boodschap zou zijn: *"Ik heb hier geen geverifieerd antwoord op. Probeer een specifiekere vraag of overleg met een collega."*
- Geen "save chat" / "export gesprek als PDF". Voor een tax-authority-tool waar antwoorden later geverifieerd moeten worden door een mens is dit een gemiste kans.
- Documenten-pagina is een platte grid — geen filter op tier/doctype, geen zoeken in titels, geen indicatie wanneer een doc is geïngest.
- Geen "belangrijk: dit is geen formeel fiscaal advies"-disclaimer. Een belastingautoriteit-tool zou dat juist hebben.

## 1.3 Backend-kwaliteit

### Sterk

- Structlog overal — JSON logs uit elke node
- Pydantic-models voor input/output
- Async / SSE-streaming
- Tier-context via header; geen ambigue defaults
- Timeouts op LLM-calls ([demo/app/pipeline/llm.py:34](demo/app/pipeline/llm.py#L34))

### Zwak / junior

- **Geen retry-backoff** bij Ollama-uitval. Eén `httpx.HTTPError` mid-stream → 500 naar de client, geen graceful degradation.
- **Geen circuit-breaker.** Als Model Runner 3× achter elkaar faalt, blijft de pipeline het proberen tot de gebruiker afhaakt.
- **Geen distributed tracing** (OpenTelemetry). Pipeline-trace zit alleen in de SSE-events; bij een crash is er geen post-mortem signaal.
- **Geen health-checks per dependency** met latency-floor: `/health` zegt alleen "connected", niet "reageerde binnen 200ms".
- **Geen idempotency-keys** op `/v1/ingest`. Dezelfde PDF tweemaal uploaden creëert een tweede chunk-set.
- **`run_crag` accepteert raw `os_client` en `redis_client`** — geen abstractielaag. Voor unit-testen is dit een probleem (mocken van een hele OpenSearch-client is veel werk; een `RetrievalPort` interface zou dit afvangen).
- **Geen rate-limiting** op de chat endpoint. Iemand kan met `curl` in een loop kosten/load opdrijven.

## 1.4 RAG-kwaliteit

### Sterk

- Citations gevalideerd tegen graded chunks (`cited_ids ⊆ relevant_ids`) — fail-closed bij INVALID_CITATIONS
- Parent-expansion: paragraph-chunk geciteerd → parent article toegevoegd aan context
- Hierarchical metadata: `parent_chunk_id`, `hierarchy_path`, deterministische chunk_id

### Zwak

- **Embedding model is e5-small (384-dim)** — terwijl de papieren architectuur e5-large (1024-dim) noemt. Voor Nederlandse juridische tekst is e5-small **niet uitvoerig gevalideerd**. Geen recall@k cijfers in de repo.
- **HyDE bestaat als file maar fired niet zichtbaar** in de demo. Tim heeft hier expliciet om gevraagd in §Module 3.
- **Query decompositie ontbreekt volledig** — de classifier herkent `COMPLEX` maar splitst niet. Tim heeft hier ook om gevraagd.
- **LLM-as-reranker is functioneel identiek aan de grader** — beide zijn één Gemma-call op de top-K met JSON-mode. De "rerank"-stap voegt latency toe zonder duidelijke meerwaarde. Een echte cross-encoder (bge-reranker-v2-m3) zou een ander signaal geven; LLM-as-reranker is een **goede laptop-keuze** maar moet als zodanig worden onderbouwd.
- **Geen recall@k of MRR-cijfers** op de golden set. Geen bewijs dat RRF k=60 beter is dan alpha-blending of pure vector. **Tim verwacht meten.**
- **Cache-threshold 0.97 is niet empirisch onderbouwd in deze repo** (alleen in de slides). Je hebt geen "we hebben getest met 0.95 / 0.97 / 0.99 en dit zijn de false-positive rates"-tabel.

## 1.5 Lokale / offline inference

### Sterk

- Docker Model Runner i.p.v. directe Ollama-call → schone OpenAI-compatible API
- `ai/gemma4:E2B` — Gemma 4 lineage, geschikt voor multilingual, klein genoeg voor laptop
- Volledig offline na warmup: WiFi uit en het werkt

### Zwak

- **Geen model-uitval-pad.** Als Model Runner crasht, geeft de API 500 i.p.v. een nette "inferentie tijdelijk onbereikbaar" boodschap.
- **Geen model-versie-pinning in de demo.** `ai/gemma4:E2B` is een tag die kan veranderen; geen SHA pinning.
- **Geen warm-up benchmark.** De gebruiker ziet een hint na 8s, maar niet *"warm-up gemiddelde 47s, koude query 28s, warme query 1.4s"*.
- **Geen alternatieve inference-pad-test** (vLLM/Mixtral als productie-equivalent). De papieren architectuur belooft dit; de demo kan het niet aantonen.

## 1.6 Ingestie-pipeline

### Sterk

- Twee chunk-strategieën: structureel (regex) + semantisch (LLM)
- Hierarchical metadata expliciet (`parent_chunk_id`, `hierarchy_path`, `article_num`, `paragraph_num`, `sub_paragraph`)
- Live SSE-stream per chunk
- AI-metadata enricher (topic, entities, summary) per chunk

### Zwak

- **Demo-corpus = 24 chunks.** Bij een live demo voelt dat als "speelgoed". Tim noemt 500K docs / 20M chunks — schaalafwijking is letterlijk 6 orders van grootte.
- **Geen deduplication** op content-hash. Twee keer dezelfde PDF uploaden = twee chunk-sets.
- **Geen graceful failure mid-ingest.** Als chunk 17 van 50 faalt op embedder-call, wat gebeurt er met chunks 18-50? Geen idee — er is geen integration-test.
- **Geen ingest-throughput cijfer.** "30 chunks in 12s" zou vertrouwen geven. Niets in de UI toont dit.
- **Geen quantization toegepast** op de embeddings (fp32 i.p.v. int8/SQ8). Bij 24 chunks irrelevant, bij 20M chunks essentieel — en Tim heeft er naar gevraagd.

## 1.7 Retrieval / generation pipeline

### Sterk

- 4 rivers (BM25 / kNN / Fusion / Rerank) zichtbaar
- Per-stage timing
- Citation validation pre-respond

### Zwak

- **Geen vergelijkende numbers**: "BM25 alone recall@5 = X, kNN alone = Y, RRF = Z". Tim's hele Module 2-vraag gaat hierover.
- **Geen ECLI-shortcut**: een query met "ECLI:NL:HR:..." zou direct via keyword-filter moeten gaan, niet via embedding. Pseudocode noemt dit; runtime niet.
- **Generation prompt wordt nergens getoond in de UI** — de assessor kan niet zien hoe je hallucinaties tegenwerkt op prompt-niveau.

## 1.8 Observability & pipeline trace

### Sterk

- Pipeline-trace per chat-turn, klikbaar naar CRAG-pagina
- 9-state diagram pulse-effect

### Zwak

- **Geen Prometheus / Grafana / structured-log dashboard**. structlog logt naar stdout; niemand aggregeert.
- **Geen TTFT-meting**. De assessment-vraag is letterlijk *"TTFT must remain low (< 1.5 seconds)"* — geen cijfer, geen drempelmarkering, geen p95-grafiek.
- **Geen request-id propagation** door de SSE-stream. Bij een failure is er geen sleutel waarmee je in logs kunt zoeken.
- **Geen audit-trail** voor wie welke geclassificeerde-tier query stelde. Voor een tax-authority is dit normaal vereist (GDPR, SOX, eigen interne audit).

## 1.9 Error handling & reliability

### Sterk

- Refuse-paden in CRAG bij IRRELEVANT, AMBIGUOUS-na-retry, INVALID_CITATIONS
- Structlog warnings bij metadata-enricher fail (graceful — chunks worden alsnog geïndexeerd)
- Timeout op LLM-calls

### Zwak

- **Geen idempotency** op POST `/v1/ingest`
- **Geen back-pressure** op chunk-stream — als de client traag is, blijft de server pushen
- **Geen graceful shutdown** bij Docker stop — open SSE-connecties krijgen connection reset
- **Geen integration tests** — alleen smoke-test in `eval_dashboard`

## 1.10 Demo-paraatheid

### Sterk

- DEMO_SCRIPT.md vers herschreven, 6 acts, ~8 min
- Slide-deck onderbouwing per Operations-tab
- Stack draait offline

### Zwak

- **Geen dress-rehearsal gedaan.** Niemand heeft de nieuwe DEMO_SCRIPT zelf doorgelopen vanuit cold-start. Risico: laatste commit ergens een UI-pad gebroken zonder dat we het weten.
- **Geen backup-screencast.** Als Docker tijdens de demo dood gaat, is er niets om naar te switchen.
- **Geen "live numbers"-pagina**. Als Tim doorvraagt "hoe weet je dat je TTFT haalt?", is er geen visuele dashboard om naartoe te wijzen.

## 1.11 Hoe goed bewijst de implementatie de assessment-eisen?

Dit is de belangrijkste vraag. Punt-voor-punt:

| Assessment-eis | Bewijs in implementatie | Verdict |
|---|---|---|
| **§1: 500K docs / 20M chunks scale** | Demo heeft 24 chunks. Drafts beschrijven de schaal. | ⚠ **Geen runtime-bewijs**, alleen papieren rekening |
| **§1: Zero-hallucination + exacte citaties** | Citation pills, hierarchy_path in metadata, validator faalt-closed | ✅ **Sterk** |
| **§1: TTFT < 1.5s** | Geen meting, geen cijfer in UI of logs | ❌ **Niet aangetoond** |
| **§Module 1: chunking-strategie** | Dual-path (structureel + semantisch), hierarchy expliciet | ✅ **Sterk** |
| **§Module 1: Vector DB + HNSW + Quantization** | OpenSearch, m=16/ef_construction=128 in mapping; **geen quantization in runtime** | ⚠ **Half — papier zegt fp16/SQ8, runtime niet** |
| **§Module 2: hybrid search + fusion strategie** | BM25 + kNN + RRF k=60, **maar zonder benchmarks** | ⚠ **Werkt, niet bewezen** |
| **§Module 2: reranking** | LLM-as-reranker (zelfde Gemma-call) | ⚠ **Pragmatisch, niet wat papier zegt** |
| **§Module 2: Top-K parameters** | top_k_bm25=20, top_k_knn=20, top_k_rerank=5, **niet zichtbaar in UI sinds CONFIG-block weg** | ⚠ **Werkt, niet meer zichtbaar** |
| **§Module 3: Query Transformation (HyDE/Decomposition)** | HyDE-file aanwezig; fired niet zichtbaar; decompositie ontbreekt | ❌ **Niet aangetoond** |
| **§Module 3: CRAG state-machine + Grader** | 9-state, drie grader-verdicts, refuse-paden | ✅ **Sterk** |
| **§Module 3: Fallback-acties op grade-uitkomst** | Tabel hoort in slides, runtime gedrag is correct | ✅ **Sterk** |
| **§Module 4: Semantic cache + cosine threshold** | 0.97, tier-gepartitioneerd, **niet empirisch onderbouwd in repo** | ⚠ **Werkt, threshold-keuze niet bewezen** |
| **§Module 4: RBAC + filter-stage** | Pre-retrieval `bool.filter` vóór scoring, mathematisch defendable | ✅✅ **Zeer sterk** |
| **§Module 4: CI/CD eval (Ragas / DeepEval)** | Ragas/DeepEval als label genoemd, alle metrics gestubd, geen CI-integratie | ❌ **Volledig gestubd — kritiek probleem** |

**Samenvattend:** **5 sterke punten, 6 grijze gebieden, 3 onbewezen.** De grijze gebieden zijn allemaal *bewijs-tekorten*, niet ontwerpfouten. Dat is goed nieuws — het kan nog. De drie onbewezen items (TTFT, HyDE/decompositie, Ragas/DeepEval) zijn het criticisme-risico.

---

# Deel 2 — Antwoorden op je 7 vragen

### Q1. Wat ziet er al sterk uit?
- 4-tier pre-retrieval RBAC met mathematische rechtvaardiging (geen ranking-leak)
- Tier-gepartitioneerde semantic cache (geen cross-tier leak via cache)
- Citation-validation als fail-closed gate
- 9-state imperatieve CRAG met expliciete refuse-paden
- Werkruimte/Operations sidebar-split — echt enterprise SaaS-pattern
- Hierarchical chunking met deterministische `chunk_id` formaat
- Streaming chat met live pipeline-trace per turn
- Volledig offline runtime na warmup, geen API-keys

### Q2. Wat voelt nog te junior of onaf?
- Eval-pagina toont **gestubd cijfers** (`stub: true` in app.js voor hallucination/toxicity/bias). Dit is het rode-vlag-moment.
- Geen TTFT-meting ondanks expliciete assessment-eis < 1.5s
- Demo-corpus van 24 chunks tegenover 20M-claim
- HyDE en decompositie genoemd in plan/papier, niet zichtbaar in demo
- Geen reproduceerbare eval-pipeline
- Geen retry/circuit-breaker rond Ollama
- Documenten-pagina is een platte lijst zonder filters
- Refuse-boodschap leest als een fout, niet als een feature

### Q3. Welke onderdelen zijn technisch impressief genoeg voor senior-niveau?
- **Pre-retrieval RBAC met `P(leak)=0` argument** — dit is het soort detail dat Tim onmiddellijk herkent als senior-thinking
- **Hierarchical metadata-design met deterministische chunk_id** — `{doc}::{art}::{lid}::{seq}` maakt re-indexing idempotent en parent-expansion O(1)
- **Tier-partitioned semantic cache** — voorkomt subtiele cross-tier leaks die de meeste candidates missen
- **Werkruimte/Operations split** — voor een "RAG-techie" niet voor de hand liggend; meer "product-architect" denken
- **CRAG met fail-closed citation-validation** — duidelijk dat je hallucinatie-control begrijpt

### Q4. Welke onderdelen missen bewijs, diepte of polish?
- **Bewijs:** TTFT-getallen, retrieval-recall-getallen, eval-getallen (alle drie ontbreken)
- **Diepte:** decompositie + HyDE actief, niet alleen genoemd
- **Polish:** refuse-flow lezen als feature niet als fout; documenten-pagina filterbaar; backup-screencast; integration-tests
- **Bewijsketen:** van golden-set → eval-run → metrics → ship/hold gate moet end-to-end werken, niet gestubd
- **Schaal-bewijs:** quantization-toggle of memory-math op live data, niet papier

### Q5. Wat zou een assessor waarschijnlijk bekritiseren?
1. *"Ragas / DeepEval staat in de UI maar ik zie het nergens runnen — laat me een echte run zien."* (gestubd)
2. *"TTFT < 1.5s — bewijs het."* (geen meting)
3. *"Je papieren design zegt LangGraph + Mixtral + e5-large + bge-reranker, je demo gebruikt geen van die. Welke is je echte design?"* (afwijking, banner mitigeert maar niet helemaal)
4. *"24 chunks. Hoe weet ik dat retrieval bij 20M chunks ook werkt?"* (geen schaal-bewijs)
5. *"HyDE en query-decompositie heb je beloofd. Waar zie ik ze firen?"* (gewoon niet zichtbaar)
6. *"Wat gebeurt er als Ollama crasht tijdens een query?"* (geen graceful degradation)
7. *"Geef me een query waarbij de grader een hallucinatie heeft tegengehouden."* (geen demo-case)
8. *"Hoe is jouw cosine 0.97 cache-threshold gevalideerd?"* (claim, geen tabel)

### Q6. Welke verbeteringen creëren het grootste "wow"-effect in een live demo?
1. **Live Ragas-run** in de Kwaliteit-tab — context_recall, faithfulness, citation_correctness updaten voor Tim's ogen op een corpus van 50 golden-queries. Eén knop, 60 seconden, echte cijfers.
2. **TTFT-badge per assistant-turn** — "TTFT 287ms ✓ < 1500ms drempel" zichtbaar onder elk antwoord. Direct antwoord op de assessment-eis.
3. **Hallucination-catch demo** — een zorgvuldig geprepareerde query waar de eerste retrieval-pass zwak is en de grader hem terecht naar AMBIGUOUS stuurt → query-rewrite → tweede pass haalt hem. *"Dit is wat fail-closed betekent."*
4. **Quantization-toggle in Operations → Ingestie** — knop "Vector quantization: fp32 / int8" → memory-math update live. Niet enkel papier.
5. **Stress-mode in Operations → Kwaliteit** — "Run 50 concurrent queries", live grafiek toont p50/p95/p99 met de 1500ms-drempellijn.
6. **Bigger corpus** — pre-flight ingestie van 5 echte Dutch tax PDFs zodat retrieval op echte diversiteit reageert tijdens demo.

### Q7. Wat moet als eerste worden gebouwd als de tijd beperkt is?
**De drie hoogste-ROI-items, in volgorde:**
1. **Echte Ragas-eval die getallen produceert** (4-6 uur). Lost het #1 criticism-risico op en geeft direct senior-gevoel.
2. **TTFT-meting + per-turn badge** (2-3 uur). Direct antwoord op de letterlijke assessment-eis.
3. **5–10 echte Dutch tax PDFs als seed** (1-2 uur ingestie + cleanup). Maakt de demo "echt" voelen.

Dat zijn ~10 uur. Daarna alles uit het "must-have"-blok hieronder, in volgorde van ROI.

---

# Deel 3 — Verbeterplan

## 3.1 Current-state samenvatting

We hebben een werkende v3-demo: streaming chat, document upload met live chunking, hiërarchische tree, RBAC tier-switch, CRAG-pipeline-trace, semantic cache. Architectuur is solide; de problemen zijn allemaal **bewijsvoering en finishing**. De UI-strip heeft het productgevoel sterk verbeterd; de slide-deck dekt de "waarom"-vragen. Wat ontbreekt is **gemeten gedrag**: geen latency, geen recall, geen eval-cijfers, geen schaal-demonstratie. Drie features die in het ontwerp zijn beloofd (HyDE, query-decomposition, kwantitatieve eval) leven nog op pseudocode-niveau.

## 3.2 Gap-analyse vs. assessment.txt

| Eis (regelnummer in assesment.txt) | Status | Wat ontbreekt |
|---|---|---|
| L17: zero-hallucination + exacte citaties | ✅ | Niets |
| L19: RBAC, helpdesk geen FIOD | ✅ | Niets |
| L21: TTFT < 1.5s op 20M chunks | ❌ | Latency-meting + schaal-bewijs |
| L31: chunking voor legal codes | ✅ | Niets |
| L33: Vector DB + HNSW + Quantization | ⚠ | Quantization in runtime |
| L37-38: hybrid + RRF/alpha keuze | ✅ | Recall@k cijfers (nice-to-have voor diepte) |
| L40: reranking + Top-K | ⚠ | Top-K weer zichtbaar in UI; LLM-rerank vs cross-encoder onderbouwen |
| L45: HyDE / Query decomposition | ❌ | Beide zichtbaar firen in demo |
| L47-51: CRAG state-machine + grader + fallbacks | ✅ | Niets |
| L54: cache + threshold | ⚠ | Empirische onderbouwing van 0.97 |
| L56: RBAC stage in pipeline | ✅✅ | Niets — dit is je sterkste punt |
| L58: CI/CD eval, Ragas + DeepEval | ❌ | Echte run, geen stubs |

**3 rode kruisen, 3 grijze, 6 groene.** Het rode kwadrant is je werkprogramma.

## 3.3 Gap-analyse vs. Tim's feedback ([TIM_FEEDBACK.md](TIM_FEEDBACK.md))

| Tim's punt | Status | Toelichting |
|---|---|---|
| Werkend product | ✅ | Demo draait |
| Wezenlijk eindproduct (geen dev-tool) | ✅ | Werkruimte/Operations split |
| Onderbouw keuzes | ⚠ | Slide-deck klaar, **niet getoetst tijdens dress-rehearsal** |
| Live demonstratie | ⚠ | Stack klaar, **geen oefenrun** |
| Chunking pipeline + AI-metadata + hiërarchie | ✅ | Sterkste pijler |
| End-to-end RAG met chat | ✅ | Sterkste pijler |

Tim's twee gele vinkjes lossen op zodra je één dress-rehearsal doet. Dat is een organisatie-issue, geen code-issue.

## 3.4 Must-have verbeteringen voor senior-niveau

Hieronder per item: **wat**, **waarom**, **welke files**, **geschatte tijd**.

### M1. Echte Ragas-eval pipeline ⏱ 4-6 uur
**Wat:** Vervang de gestubde metrics in `app.js` (regel 1276-1280) door echte Ragas-runs.

**Waarom:** Tim's #1 vraag in §Module 4 is letterlijk *"hoe evalueer je automatisch?"*. Gestubde cijfers zijn het zwaarste senior-killer-signaal in de demo.

**Files:**
- nieuwe file `demo/app/eval/ragas_runner.py` — wrapt `ragas` library, draait `context_recall`, `faithfulness`, `answer_relevancy` op de golden set
- nieuwe file `demo/app/eval/deepeval_runner.py` — `HallucinationMetric`, `BiasMetric` per query
- update [demo/app/routers/eval_dashboard.py](demo/app/routers/eval_dashboard.py): nieuwe endpoint `POST /v1/eval/run` die de runners aanroept en resultaten cached
- update [demo/app/static/app.js](demo/app/static/app.js): vervang stub-metrics door fetch van `/v1/eval/run`; verwijder `stub: true` flags
- expand [eval/golden_test_set_spec.json](eval/golden_test_set_spec.json) van 5 → 25 entries (5 per query-archetype: SIMPLE, COMPLEX, ECLI, ADVERSARIAL, CROSS-TIER)
- update `demo/requirements-demo.txt` — voeg `ragas` en `deepeval` toe

**Risico:** Ragas roept de LLM aan voor scoring. Met `ai/gemma4:E2B` als evaluator kan dit minder accuraat zijn. **Mitigatie:** gebruik dezelfde evaluator-LLM en documenteer dit ("eval gebruikt dezelfde Gemma 4 als de generator; productie zou GPT-4 als external judge gebruiken"). Dit is intellectueel eerlijk, geen probleem.

### M2. TTFT-meting + per-turn badge ⏱ 2-3 uur
**Wat:** Time-to-first-token meten en per assistant-turn tonen als pill ("TTFT 287ms ✓").

**Waarom:** De assessment-eis is letterlijk *"TTFT < 1.5s"*. Geen meting = onverdedigbaar.

**Files:**
- update [demo/app/routers/chat.py](demo/app/routers/chat.py): track `time.perf_counter()` bij request-start en eerste `token`-event; emit een `{type:"ttft", ms: <int>}` SSE-event
- update [demo/app/static/app.js](demo/app/static/app.js): in `streamChat`, render een TTFT-badge bovenaan elke assistant-bubble met groen/amber/rood threshold-coloring (≤500ms groen, ≤1500ms amber, >1500ms rood)
- update [demo/app/pipeline/cache.py](demo/app/pipeline/cache.py): emit TTFT-event bij cache-HIT met `cache_ttft_ms`

**Bonus:** rolling p50/p95/p99 in een nieuwe widget op de Kwaliteit-tab. Aggregatie in Redis sliding window.

### M3. Bigger seed corpus (5-10 echte Dutch tax PDFs) ⏱ 1-2 uur
**Wat:** Pre-ingest een handvol echte (publiek beschikbare) Dutch tax-documenten zodat retrieval bij demo-tijd op echte diversiteit reageert.

**Waarom:** 24 chunks voelen als speelgoed. Tim noemt 500K docs. Een gat van 6 orden is te groot om in een demo te overbruggen; een gat van 4 orden (10 docs × 30 chunks ≈ 300 chunks) is overtuigender.

**Files:**
- maak `demo/seed_data/pdfs/` met 5-10 publieke documenten (Wet IB 2001 hoofdstukken, recente ECLI-uitspraken, fiscale beleidsmemo's, FIOD-procedurehandleidingen)
- nieuwe script `demo/scripts/preingest.sh` — pre-ingest tijdens `docker compose up`
- mark 1-2 documents als `CLASSIFIED_FIOD` zodat de RBAC-demo concrete content heeft

**Risico:** copyright. Mitigatie: alleen publieke documenten van wetten.overheid.nl en uitspraken.rechtspraak.nl.

### M4. HyDE actief in demo ⏱ 2 uur
**Wat:** Voor `SIMPLE` queries met lage retrieval-confidence: HyDE-pad inschakelen en zichtbaar in pipeline-trace.

**Waarom:** Tim's §Module 3 vraag.

**Files:**
- update [demo/app/pipeline/retriever.py](demo/app/pipeline/retriever.py): als BM25-top1 score < threshold OF kNN-top1 cosine < 0.55, roep HyDE aan via [demo/app/pipeline/hyde.py](demo/app/pipeline/hyde.py) en gebruik die embedding als kNN-query
- update [demo/app/pipeline/crag.py](demo/app/pipeline/crag.py): emit `{node:"hyde", result:<hypothesis_preview>, duration_ms:N}` SSE-event
- update CRAG-state-diagram in [demo/app/static/app.js](demo/app/static/app.js): voeg een HyDE-knoop toe tussen `classify_query` en `retrieve`

### M5. Query decompositie actief in demo ⏱ 3-4 uur
**Wat:** Voor `COMPLEX` queries: splits in 2-3 sub-queries, retrieve elk, merge resultaten.

**Waarom:** Tim's §Module 3 vraag.

**Files:**
- update [demo/app/pipeline/classifier.py](demo/app/pipeline/classifier.py): bij `COMPLEX` ook een lijst sub-queries teruggeven
- update [demo/app/pipeline/retriever.py](demo/app/pipeline/retriever.py): als sub-queries aanwezig, doe parallel retrieval, merge via RRF over sub-query-resultaten
- update [demo/app/pipeline/crag.py](demo/app/pipeline/crag.py): emit `{node:"decompose", sub_queries:[...]}` SSE-event
- update prompt: gebruik [prompts/decomposition_prompt.txt](prompts/decomposition_prompt.txt) als basis

### M6. Quantization-toggle of -bewijs in Ingestie ⏱ 2-3 uur
**Wat:** Op de Ingestie-pagina een widget die memory-impact toont voor fp32 / fp16 / int8 quantization op het huidige corpus, plus optie om actief te re-quantiseren via OpenSearch reindex.

**Waarom:** Tim's §Module 1 noemt expliciet *"Quantization to prevent OOM"*. De papieren versie heeft dit; de demo niet.

**Files:**
- nieuwe component in [demo/app/static/index.html](demo/app/static/index.html): `<section data-workspace="ingest">` voeg quantization-widget toe (3 cards met memory-getallen, lid-toggle voor active mode)
- nieuwe endpoint `POST /v1/admin/reindex_quantized` — herbouwt de OpenSearch index met aangevraagde precision
- wel/niet implementeren-mogelijkheid: alleen de **memory-math widget** is goedkoop (<1u); de actuele reindex is duur. Aanbeveling: alleen memory-widget + slide voor productie.

### M7. Reliability: graceful degradation op Model Runner failure ⏱ 3-4 uur
**Wat:** Circuit-breaker rond Ollama-calls; als 3 consecutive failures, stuur "inferentie tijdelijk onbereikbaar — refuse" naar de gebruiker i.p.v. 500.

**Waarom:** Senior signal. Tim verwacht productiedenken.

**Files:**
- nieuwe file `demo/app/pipeline/breaker.py` — eenvoudige circuit-breaker (open/half-open/closed states, threshold 3 failures / 30 sec)
- update [demo/app/pipeline/llm.py](demo/app/pipeline/llm.py): wrap alle LLM-calls in de breaker
- update [demo/app/pipeline/crag.py](demo/app/pipeline/crag.py): bij `BreakerOpen` exception → REFUSE met "service tijdelijk onbeschikbaar"

### M8. Refuse-flow als feature framen ⏱ 30 min
**Wat:** Verbeter de refuse-tekst in de UI van een dor "geen relevante context" naar een productieve boodschap.

**Files:**
- update [demo/app/pipeline/generator.py](demo/app/pipeline/generator.py) refuse_response generator: gebruik tier-aware Nederlandse boodschap met suggestie

```
"Ik heb geen geverifieerd antwoord op deze vraag binnen jouw toegangsniveau.
Probeer een specifiekere formulering, of overleg met een collega met hogere
toegang. Deze vraag is gelogd in de audit-trail."
```

- update [demo/app/static/app.js](demo/app/static/app.js): style refuse-bubbles met amber border ipv error-rood, label "Gefilterd antwoord" bovenaan

### M9. Top-K config terug in Retrieval-tab ⏱ 1 uur
**Wat:** De CONFIG-pill (top_k_bm25, top_k_knn, rrf_k, top_k_rerank) is in de strip-down weggehaald. Dat was een fout — Tim vraagt hier expliciet naar in §Module 2.

**Files:**
- update [demo/app/static/index.html](demo/app/static/index.html) Retrieval-section: kleine pill-row "BM25 top-20 · kNN top-20 · RRF k=60 · Rerank top-5"
- update [demo/app/static/app.js](demo/app/static/app.js) `renderRetrievalTrace`: lees waarden uit response, toon in pill-row

Geen formules, geen "waarom"-tekst — pure params, één regel hoog. Past bij de enterprise-feel.

### M10. Audit-trail per query ⏱ 2-3 uur
**Wat:** Elke query → log-record in Redis sorted set met `{ts, session_id, tier, query, grade, citations, ttft_ms}`. Toegankelijk via Operations → Toegang.

**Waarom:** Senior signal voor een tax-authority-tool. Tim weet dat audit-trail een productie-vereiste is voor RBAC-systemen.

**Files:**
- nieuwe file `demo/app/audit.py` — `log_query()` helper, redis sorted set per dag
- update [demo/app/pipeline/crag.py](demo/app/pipeline/crag.py): roep `log_query` aan in respond/refuse paths
- update [demo/app/static/index.html](demo/app/static/index.html): nieuwe "Audit-trail"-tabel onderaan Toegang-pagina, laatste 50 queries

### M11. Dress-rehearsal + screencast ⏱ 1.5 uur
**Wat:** Loop de DEMO_SCRIPT.md één keer cold-start door, neem op (OBS / Loom), commit naar `demo/recordings/dress_rehearsal_v3.mp4` (en update `.gitignore` als file >100MB → gebruik git-lfs of verwijs naar externe link).

**Waarom:** Tim's punt 4 — "live demonstratie". Geen verzekering = roulette.

## 3.5 Nice-to-have

| Item | Waarde | Tijd |
|---|---|---|
| Stress-mode: 50 concurrent queries → live p50/p95/p99 grafiek op Kwaliteit-tab | Hoog | 4-6u |
| Cosine threshold-tuning UI: slider + live false-positive examples | Middel | 2u |
| ECLI-shortcut: query met `ECLI:NL:` → direct keyword filter, geen embedding | Middel | 1-2u |
| Document-pagina: filter op tier/doctype + zoeken in titels | Laag | 2u |
| Dark/light theme toggle | Laag | 1u |
| Export gesprek als PDF met citaties | Middel | 2-3u |
| Disclaimer-banner "geen formeel fiscaal advies" | Laag | 30m |
| Cross-encoder reranker als optie naast LLM-rerank | Middel | 3u |
| OpenTelemetry tracing met Jaeger lokaal | Hoog | 4-5u |
| GitHub Actions workflow die golden-set runt op elke PR | Hoog | 2-3u |

## 3.6 Technische roadmap (volgorde)

**Sprint 1 — Bewijs (8-12 uur):** M1, M2, M3 — Ragas-eval, TTFT-meting, bigger corpus.
*Outcome: drie van Tim's harde vragen hebben kwantitatieve antwoorden.*

**Sprint 2 — Compleetheid (6-8 uur):** M4, M5, M9 — HyDE actief, decomposition actief, Top-K config zichtbaar.
*Outcome: drie ontbrekende §Module 2/3 features zichtbaar firende.*

**Sprint 3 — Polish & reliability (5-8 uur):** M6, M7, M8, M10 — quantization-widget, circuit-breaker, refuse-framing, audit-trail.
*Outcome: senior-signaal door productie-denken visible.*

**Sprint 4 — Demo-paraatheid (1.5-2 uur):** M11 — dress-rehearsal + screencast.
*Outcome: niemand wordt overrompeld op de dag.*

**Totaal: 20-30 uur** voor must-haves. Realistisch in 3-5 werkdagen.

## 3.7 Voorgestelde file/module-wijzigingen (samenvatting)

```
demo/
├── app/
│   ├── audit.py                              # NEW (M10)
│   ├── eval/
│   │   ├── __init__.py                       # NEW
│   │   ├── ragas_runner.py                   # NEW (M1)
│   │   └── deepeval_runner.py                # NEW (M1)
│   ├── pipeline/
│   │   ├── breaker.py                        # NEW (M7)
│   │   ├── classifier.py                     # EDIT — emit sub_queries voor COMPLEX (M5)
│   │   ├── crag.py                           # EDIT — HyDE/decompose/breaker events (M4,M5,M7)
│   │   ├── generator.py                      # EDIT — betere refuse-tekst (M8)
│   │   ├── hyde.py                           # EDIT — wired in retriever (M4)
│   │   ├── llm.py                            # EDIT — wrap in breaker (M7)
│   │   └── retriever.py                      # EDIT — HyDE + decompose paths (M4,M5)
│   ├── routers/
│   │   ├── chat.py                           # EDIT — emit ttft event (M2)
│   │   └── eval_dashboard.py                 # EDIT — POST /v1/eval/run (M1)
│   └── static/
│       ├── app.js                            # EDIT — TTFT badge, real metrics, refuse-styling (M1,M2,M8,M9)
│       └── index.html                        # EDIT — Top-K pill, quantization widget, audit-tabel (M6,M9,M10)
├── eval/
│   └── golden_test_set_spec.json             # EXPAND naar 25 entries (M1)
├── seed_data/
│   └── pdfs/                                 # NEW — 5-10 echte PDFs (M3)
├── scripts/
│   └── preingest.sh                          # NEW (M3)
├── recordings/
│   └── dress_rehearsal_v3.mp4                # NEW (M11)
└── requirements-demo.txt                     # EDIT — voeg ragas, deepeval toe (M1)
```

## 3.8 Demo-storyline voor de presentatie (8-9 minuten)

Herschrijven van DEMO_SCRIPT.md ná Sprint 1-3 om de nieuwe features te tonen. Voorlopige flow:

**Openingsbeeld:** browser op `localhost:8000`, Werkruimte → Gesprek, splash voorbij. Disclaimer-bannertje onderaan ("geen formeel fiscaal advies").

**Act 1 — Eerste vraag + TTFT-bewijs (0:00-1:15)**
Suggested prompt klikken. Tokens streamen. **Bovenaan de bubble verschijnt "TTFT 287ms ✓".**
*Talking point: "Het TTFT-budget uit het assessment is 1500ms. Dit is een warm-cache call op een laptop-Gemma — productie met vLLM zit in single-digit hundreds."*

**Act 2 — Hallucination-catch (1:15-2:30)**
Stel een geprepareerde "trick query" — een echte Dutch tax-vraag waar de eerste retrieval zwak zou zijn. Pipeline-trace toont AMBIGUOUS → rewrite → retry → RELEVANT. CRAG-tab open: pad licht op via `grade_context` → `rewrite_and_retry` → `grade_context` → `respond`.
*Talking point: "De grader is de fail-closed gate. Bij IRRELEVANT had hij naar refuse gegaan."*

**Act 3 — HyDE in actie (2:30-3:30)**
Stel een SIMPLE-maar-paraphrase query waar BM25 het niet redt. Pipeline-trace toont expliciet `hyde` node met de hypothetische passage als preview.
*Talking point: "Standaard kNN op een korte query mist context. HyDE laat de LLM eerst een hypothetisch antwoord genereren, embedt dat, en gebruikt die richtingsvector voor kNN. Top-1 cosine springt van 0.42 naar 0.71."*

**Act 4 — Live ingestie + hiërarchie + retrieval-highlight (3:30-5:30)**
PDF slepen, chunks streamen, tree opent, vraag stellen, tree-nodes pulsen.
*Talking point: "Dit is Tim's letterlijke punt — metadata voor hiërarchische relaties — gebouwd in 30 seconden, parent-expansion zichtbaar."*

**Act 5 — RBAC tier-switch (5:30-6:30)**
Publiek → vraag → refuse. FIOD-rechercheur → zelfde vraag → antwoord met FIOD-citaten. Toegang-tab: `bool.filter` is niet meer rauw maar visueel. Onder in audit-trail-tabel verschijnt het query-record (uit M10).
*Talking point: "Filter staat vóór scoring — `P(leak) = 0`. Cache is tier-gepartitioneerd. Audit-trail is verplicht voor productie."*

**Act 6 — Live Ragas-run (6:30-8:00)**
Spring naar Operations → Kwaliteit. Klik "Run golden set". 25 queries beginnen door de pipeline te lopen, metric-cards updaten live (context_recall: 0.78 → 0.84 → 0.91, faithfulness: 0.92 → 0.94 → 0.94). Ship/hold-pills springen op groen.
*Talking point: "Dit is wat een CI/CD-eval-gate is. Bij elke nieuwe model-versie draait dit voor een PR mergt. Drempels uit assessment-eis: faithfulness ≥ 0.90, context_precision ≥ 0.85."*

**Act 7 — Cache-hit re-ask (8:00-8:30)**
Eerste vraag herhalen. **TTFT-badge: "TTFT 12ms ✓"**.
*Talking point: "Cache-key is een embedding, niet een hash. Tier-gepartitioneerd zoals net getoond."*

**Act 8 — Onderbouwingsslides (8:30-9:00)**
Spring naar het deck (`slides/output/operations_justification.pptx`). Tim mag kiezen welke Operations-tab hij wil onderbouwd zien — ik open de bijbehorende slide.
*Talking point: "Het product zelf is gestript van uitleg. De onderbouwing leeft hier. Vraag erop door."*

## 3.9 Risico's en mitigaties

| Risico | Kans | Impact | Mitigatie |
|---|---|---|---|
| Ragas-runs duren te lang op laptop (>3 min) | Hoog | Middel | Cache laatste run; "Run" toont historische cijfers + button "refresh"; achtergrond-job tijdens andere acts |
| TTFT-meting toont >1500ms op koude eerste call | Zeker | Hoog | Pre-warm in pre-flight checklist; live demo doet GEEN cold start |
| Bigger corpus PDFs hebben copyright-issues | Laag | Hoog | Alleen wetten.overheid.nl + uitspraken.rechtspraak.nl |
| HyDE/decompositie produceert slechtere resultaten dan baseline op 24-chunk corpus | Middel | Middel | Test op golden set, valideer dat metrics niet zakken; toggle uitschakelbaar |
| Demo-laptop crasht / Docker freezes mid-demo | Laag | Catastrofaal | Backup-screencast (M11); secondary laptop met dezelfde stack klaar |
| Tim graaft door in `drafts/final_submission_v2.md` en wijst op LangGraph-divergentie | Middel | Middel | Banner in v2 staat al; aan het begin zelf benoemen ("dit is productie-design vs laptop-implementatie, bewust gekozen") |
| Grader-prompt produceert inconsistente verdicts | Middel | Middel | Pin op temperature=0; toon prompt in slides; eval-runner valideert consistency |
| Eval-getallen zijn onverwacht laag bij Tim's demo (b.v. faithfulness 0.72) | Middel | Hoog | Run vooraf, weet wat de getallen zijn, kalibreer drempels accordingly; framing: "we hebben strenge drempels gekozen, een lager getal is informatie, niet een falen" |

## 3.10 Final pre-demo checklist (1 dag voor)

- [ ] Sprint 1 + 2 + 3 alle 11 must-haves landed in master
- [ ] Dress-rehearsal cold-start uitgevoerd; alle 8 acts doorlopen zonder hapering
- [ ] Backup-screencast opgenomen in `demo/recordings/`
- [ ] `docker compose up -d`, 30 sec warmup, alle 6 splash-stages groen
- [ ] `curl localhost:8000/health` → `warmup_complete: true`
- [ ] Pre-warm cache met 8 demo-queries (één per archetype)
- [ ] Pre-ingest 5-10 echte Dutch tax PDFs in seed corpus
- [ ] Ragas-run draaien; expected metrics genoteerd (faithfulness, context_recall, citation_correctness)
- [ ] Slide-deck `assessment_AI_USE_emresemerci.pptx` met `slides/output/operations_justification.pptx` integreerd
- [ ] WiFi uit; alle externe URL's in browser-tabs vooraf geladen
- [ ] Externe monitor mirrored, niet extended
- [ ] Slack / Teams / IDE / heavy apps gesloten
- [ ] Browser op 100% zoom, incognito
- [ ] Hard refresh (Ctrl+Shift+R) op localhost:8000 — assets `?v=15` actief
- [ ] [DEMO_SCRIPT.md](demo/DEMO_SCRIPT.md) geprint of open op tweede device
- [ ] [TIM_FEEDBACK.md](TIM_FEEDBACK.md) en [SENIOR_REVIEW_AND_PLAN.md](SENIOR_REVIEW_AND_PLAN.md) klaar om te delen op verzoek
- [ ] Stack één keer restart-test: `docker compose down && up -d` → werkt nog steeds binnen 60 sec

## 3.11 Wat hier niet in staat

Bewust uit dit plan gehouden:
- **vLLM / Mixtral integratie.** Te duur voor de tijdsbudget; beter als toekomst-werk in slides aanstippen.
- **Multi-node OpenSearch cluster.** Productie-architectuur. Beter geïllustreerd via [performance/resource_allocation.md](performance/resource_allocation.md) tijdens Q&A.
- **Cross-encoder reranker (bge-reranker-v2-m3).** Kost geheugen op laptop; LLM-as-reranker is intentioneel pragmatisch en moet zo worden onderbouwd in slide 2.
- **Echte JWT/OIDC auth.** Tier-context per request is voldoende voor de demo; productie-auth is buiten scope.
- **OpenTelemetry / Grafana volledige observability stack.** Nice-to-have, maar circuit-breaker + audit-trail dekt het senior-signaal in beperktere tijd.

---

# Slot

Het werk is overzichtelijk:
- **3 hoogste-ROI features** (M1, M2, M3 — Ragas, TTFT, bigger corpus) zetten de assessor op een ander been: van *"klopt het, maar bewijs het"* naar *"oké, het is bewezen"*.
- **3 ontbrekende features** (M4, M5, M9 — HyDE, decompositie, Top-K weer zichtbaar) sluiten de §Module 2/3 gaten.
- **3 productie-signalen** (M6, M7, M8, M10 — quantization, circuit-breaker, refuse-framing, audit-trail) zijn de senior-signalen.
- **1 organisatie-item** (M11 — dress-rehearsal) is het verschil tussen een afgeraffelde en een zelfverzekerde live demo.

20-30 uur werk; binnen een week haalbaar. Daarna ben je niet meer junior/medior; daarna laat je echt zien wat een senior architect doet.
