# Enterprise RAG Architecture — Dutch Tax Authority

Technical assessment door **Emre Ram**.

Twee lagen in deze repo:

- **Live demo** in [`demo/`](demo/) — een werkend product op je laptop, volledig offline na warmup. Dit is wat je tijdens het gesprek live ziet.
- **Schriftelijke architectuur** in [`drafts/`](drafts/), [`pseudocode/`](pseudocode/), [`schemas/`](schemas/), [`diagrams/`](diagrams/), [`performance/`](performance/) — het ontwerpdocument voor productie-schaal (20M chunks, GPU-cluster). De demo is een gereduceerde implementatie ervan; de afwijkingen staan in de banner van [`drafts/final_submission_v2.md`](drafts/final_submission_v2.md).

---

## Begin hier

1. **Open het deck:** [`assessment_AI_USE_emresemerci.pptx`](assessment_AI_USE_emresemerci.pptx). Geeft het architectuur-overzicht en de aanpak.
2. **Onderbouwingsslides:** [`slides/output/operations_justification.pptx`](slides/output/operations_justification.pptx) — vijf slides over de keuzes per Operations-tab in de demo. Bron-markdown in [`slides/operations_justification.md`](slides/operations_justification.md).
3. **Hoofd-document:** [`drafts/final_submission_v2.md`](drafts/final_submission_v2.md). Vier modules in detail.
4. **Live demo:** zie de instructies hieronder.

## Live demo (Docker)

Vereisten: **Docker Desktop 4.40+** met **Model Runner** ingeschakeld (verschijnt onder *Settings → Features in development → Beta features → Enable Docker Model Runner*). Geen API-keys, geen netwerk-call tijdens runtime.

```bash
cd demo
docker compose up -d
# Wacht ~30 sec op de warmup (embedding-model + index + cache).
# Open vervolgens:
open http://localhost:8000
```

De eerste run pulled `ai/gemma4:E2B` (~1.5 GB) via Model Runner. Volgende starts zijn instant.

**Wat je in de browser ziet:**

- **Werkruimte** (eindgebruiker): Gesprek + Documenten.
- **Operations** (operator/engineer): Ingestie · Retrieval · CRAG-pipeline · Toegang · Kwaliteit.
- Wissel rol linksboven (Publiek / Juridisch / Inspecteur / FIOD) om RBAC live te zien werken.

**Demo-flow:** [`demo/DEMO_SCRIPT.md`](demo/DEMO_SCRIPT.md) — 6 acts à ~80 sec, klaar om langs te lopen.

## Stack van de live demo

| Onderdeel | Keuze | Waarom (één regel) |
|---|---|---|
| Inferentie | Docker Model Runner · `ai/gemma4:E2B` | Lokaal, geen API-key, OpenAI-compatible endpoint |
| Embeddings | `intfloat/multilingual-e5-small` (384-dim) | CPU-snel, multilingual incl. Nederlands |
| Vector + BM25 | OpenSearch 2.15 · HNSW (m=16, ef=128) | Eén engine voor hybride zoek + filter |
| Cache | Redis Stack | Tier-gepartitioneerd, semantisch (cosine ≥ 0.97) |
| API | FastAPI + SSE | Streaming chat, live trace per turn |
| Frontend | Tailwind · vanilla JS | Geen build-stap, één HTML + JS bestand |

Dependency-lijst voor de demo: [`demo/requirements-demo.txt`](demo/requirements-demo.txt).

## Productie-architectuur (papieren versie)

Het ontwerp dat in de drafts staat is gebouwd voor **20M chunks** op een **3-node OpenSearch-cluster + GPU-LLM** (Mixtral / Llama 3.1 70B). De demo gebruikt een lichtere stack zodat hij op een normale laptop draait. De architectonische keuzes (RRF k=60, pre-retrieval RBAC, MAX_RETRIES=1, CRAG-grading, parent-expansion, semantic cache) zijn in beide identiek.

Ondersteunende artefacten:
- [`pseudocode/`](pseudocode/) — 5 bestanden: ingestion, retrieval, CRAG, grader, cache
- [`schemas/`](schemas/) — chunk-metadata (22 velden), OpenSearch index mapping, RBAC-rollen
- [`diagrams/`](diagrams/) — architectuur, retrieval-flow, CRAG-states, security-model
- [`prompts/`](prompts/) — grader / generator / HyDE / decomposition prompt-templates
- [`eval/`](eval/) — golden test-set spec + metrics matrix
- [`performance/resource_allocation.md`](performance/resource_allocation.md) — sizing en cost per query op productieschaal
- [`reference/assumptions.md`](reference/assumptions.md) — A1–A18 aannames

## Project-bestanden

| Bestand | Doel |
|---|---|
| [`assesment.txt`](assesment.txt) | De originele opdracht zoals ontvangen |
| [`TIM_FEEDBACK.md`](TIM_FEEDBACK.md) | Letterlijke feedback uit de eerste assessment-ronde |
| [`SENIOR_LEVEL_PLAN.md`](SENIOR_LEVEL_PLAN.md) | Plan voor de post-feedback refactor |
| [`OUTDATED_AUDIT.md`](OUTDATED_AUDIT.md) | Overzicht van wat in deze repo v1-design is en wat v3-implementatie |
| [`CLAUDE.md`](CLAUDE.md) | Behavioural guidelines die ik bij het werken aanhield |
