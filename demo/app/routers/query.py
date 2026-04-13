import uuid
from fastapi import APIRouter, Request, HTTPException
from app.models import QueryRequest, QueryResponse
from app.pipeline.crag import run_crag
import structlog

log = structlog.get_logger()
router = APIRouter()


@router.post("/query", response_model=QueryResponse, summary="Run the CRAG pipeline on a tax query")
async def query_endpoint(request: Request, body: QueryRequest):
    session_id = body.session_id or str(uuid.uuid4())[:8]
    log.info("query_received", query=body.query[:80], tier=body.security_tier, session=session_id)

    try:
        result = await run_crag(
            query=body.query,
            security_tier=body.security_tier,
            session_id=session_id,
            os_client=request.app.state.opensearch,
            redis_client=request.app.state.redis,
        )
        return result
    except Exception as e:
        log.error("pipeline_error", error=str(e))
        raise HTTPException(status_code=500, detail=f"Pipeline error: {str(e)}")
