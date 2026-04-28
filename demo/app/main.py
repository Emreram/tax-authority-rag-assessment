from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from redis import Redis
from app.config import get_settings
from app.opensearch.client import get_opensearch_client
from app.opensearch.setup import setup_opensearch
from app.pipeline import embedder
from app.pipeline.llm import ping as llm_ping
from app.routers import query, health, cache, chat, ingest, eval_dashboard
import structlog

log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.warmup_complete = False
    app.state.warmup_stage = "starting"
    log.info("startup_begin", llm=settings.llm_model, embedding=settings.embedding_model)

    # Preload embedder (downloads e5-small on first boot, then cached)
    app.state.warmup_stage = "loading_embedder"
    embedder.preload()
    log.info("embedder_ready", dim=settings.embedding_dim)

    # Verify LLM (Docker Model Runner) reachable
    app.state.warmup_stage = "pinging_llm"
    if not await llm_ping():
        log.warning("llm_unreachable_at_startup", base_url=settings.llm_base_url)

    # Connect OpenSearch + seed data
    app.state.warmup_stage = "opensearch_setup"
    app.state.opensearch = get_opensearch_client()
    await setup_opensearch()

    # Connect Redis
    app.state.warmup_stage = "connecting_redis"
    app.state.redis = Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        decode_responses=True,
    )
    app.state.redis.ping()
    log.info("redis_connected")

    app.state.warmup_complete = True
    app.state.warmup_stage = "ready"
    log.info("startup_complete")
    yield

    log.info("shutdown")


app = FastAPI(
    title="Enterprise RAG — Dutch Tax Authority (Live Demo)",
    description="""
## CRAG Pipeline Demo — Dutch Tax Authority

A working implementation of the **Corrective RAG** architecture designed for the
Dutch Tax Authority (Belastingdienst). Built as part of a Lead AI Engineer assessment.

---

### Architecture
This demo runs a real pipeline with:
- **OpenSearch 2.15** — hybrid BM25 + kNN search with Dutch legal analyzer (HNSW m=16)
- **Redis** — semantic cache with security-tier partitioning
- **Ollama (Gemma 3 / qwen2.5:3b on-device)** — query classification, retrieval grading, answer generation with citations
- **sentence-transformers (intfloat/multilingual-e5-small, 384-dim)** — in-process Dutch embeddings
- **RRF fusion** — Reciprocal Rank Fusion (k=60) combining sparse and dense retrieval
- **CRAG state machine** — 8 states with grading gate before generation

### State Machine Flow
```
cache_lookup → classify → retrieve → grade
  RELEVANT         → generate → validate → respond
  AMBIGUOUS (r<1)  → rewrite → retrieve → grade (retry)
  IRRELEVANT       → refuse
```

### RBAC Security Tiers
| Tier | Documents accessible |
|------|---------------------|
| **PUBLIC** | Public legislation (Wet IB, Wet OB, Successiewet), published case law |
| **INTERNAL** | + Internal policies, Handboek Invordering, e-learning materials |
| **RESTRICTED** | + Transfer pricing richtlijnen, audit methodologies |
| **CLASSIFIED_FIOD** | + FIOD opsporingshandleidingen, fraud investigation docs |

### Demo Queries to Try
| Query | Tier | Expected behaviour |
|-------|------|--------------------|
| `Wat is de arbeidskorting voor 2024?` | PUBLIC | RELEVANT → RESPOND |
| `Wat zijn de BTW-tarieven in Nederland?` | PUBLIC | RELEVANT → RESPOND |
| `ECLI:NL:HR:2021:1523` | PUBLIC | REFERENCE → exact-ID shortcut |
| `Hoe werkt transfer pricing onderzoek?` | INTERNAL | AMBIGUOUS (limited docs) |
| `Hoe werkt transfer pricing onderzoek?` | RESTRICTED | RELEVANT (more docs visible) |
| `Wat zijn de FIOD opsporingsmethoden voor BTW-fraude?` | CLASSIFIED_FIOD | RELEVANT |
| `Who built the Eiffel Tower?` | PUBLIC | IRRELEVANT → REFUSE |

### Second Call = Cache
Send the same query twice — the second call returns from Redis cache (~10ms vs ~2-3s).
The `source` field in the response shows `"cache"` vs `"pipeline"`.

---

*Assessment by Emre Ram — Lead AI Engineer Technical Assessment*
""",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

app.include_router(query.router, prefix="/v1", tags=["Query"])
app.include_router(chat.router, prefix="/v1", tags=["Chat"])
app.include_router(ingest.router, prefix="/v1", tags=["Ingest"])
app.include_router(eval_dashboard.router, tags=["Eval"])
app.include_router(health.router, tags=["Health"])
app.include_router(cache.router, prefix="/v1", tags=["Cache"])

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

NO_CACHE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}


@app.get("/", include_in_schema=False)
async def root():
    return FileResponse(STATIC_DIR / "index.html", headers=NO_CACHE_HEADERS)


@app.middleware("http")
async def static_no_cache(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        for k, v in NO_CACHE_HEADERS.items():
            response.headers[k] = v
    return response
