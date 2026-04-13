# Dutch Tax Authority RAG — Live Demo

A working CRAG pipeline with OpenSearch, Redis, and Google Gemini.

## Prerequisites

- Docker Desktop (running)
- Internet connection (Gemini API calls)

## Run it

```bash
cd demo
docker-compose up --build
```

First run takes ~3-5 minutes:
- OpenSearch starts up (~60s)
- 24 Dutch tax law chunks get embedded via Gemini and indexed into OpenSearch

Once running, open: **http://localhost:8000/docs**

## Try these queries

Open `POST /v1/query` in the Swagger UI and try:

**Basic tax query (PUBLIC tier):**
```json
{
  "query": "Wat is de arbeidskorting voor 2024?",
  "security_tier": "PUBLIC"
}
```

**BTW rates:**
```json
{
  "query": "Wat zijn de BTW-tarieven in Nederland?",
  "security_tier": "PUBLIC"
}
```

**Exact case law reference:**
```json
{
  "query": "ECLI:NL:HR:2021:1523",
  "security_tier": "PUBLIC"
}
```

**RBAC demo — same query, different access:**
```json
{ "query": "Hoe werkt transfer pricing onderzoek?", "security_tier": "INTERNAL" }
```
Then try with `"security_tier": "CLASSIFIED_FIOD"` — more documents become visible.

**Cache demo:**
Send any query twice. The second call returns in ~10ms with `"source": "cache"`.

**Refusal demo:**
```json
{
  "query": "Who built the Eiffel Tower?",
  "security_tier": "PUBLIC"
}
```

## What the response shows

Every response includes:
- `response` — the answer with inline citations `[Source: chunk_id | path]`
- `citations` — structured source list
- `source` — `"pipeline"` or `"cache"`
- `pipeline_trace` — every CRAG state visited with timing
- `timing` — per-stage latency breakdown

## Other endpoints

- `GET /health/detailed` — service status + config
- `GET /v1/cache/stats` — cache entries by tier
- `GET /health/pipeline` — state machine architecture info

## Stop

```bash
docker-compose down
```

To also remove the OpenSearch data volume:
```bash
docker-compose down -v
```
