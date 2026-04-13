from pydantic import BaseModel, Field
from typing import Optional
from enum import Enum


class SecurityTier(str, Enum):
    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    RESTRICTED = "RESTRICTED"
    CLASSIFIED_FIOD = "CLASSIFIED_FIOD"


class QueryRequest(BaseModel):
    query: str = Field(..., min_length=3, max_length=2000, description="The tax-related question")
    security_tier: SecurityTier = Field(
        SecurityTier.PUBLIC,
        description="User's security clearance tier. Controls which documents are accessible."
    )
    session_id: Optional[str] = Field(None, description="Optional session identifier")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "query": "Wat is de arbeidskorting voor 2024?",
                    "security_tier": "PUBLIC",
                    "session_id": "demo-session-001"
                },
                {
                    "query": "Wat zijn de BTW-tarieven in Nederland?",
                    "security_tier": "INTERNAL",
                    "session_id": "demo-session-002"
                },
                {
                    "query": "ECLI:NL:HR:2021:1523",
                    "security_tier": "PUBLIC",
                    "session_id": "demo-session-003"
                },
                {
                    "query": "Hoe werkt transfer pricing onderzoek?",
                    "security_tier": "CLASSIFIED_FIOD",
                    "session_id": "demo-session-004"
                }
            ]
        }
    }


class Citation(BaseModel):
    chunk_id: str
    hierarchy_path: str
    title: str
    article_ref: Optional[str] = None
    effective_date: Optional[str] = None


class PipelineStep(BaseModel):
    node: str
    result: Optional[str] = None
    detail: Optional[str] = None
    duration_ms: float


class TimingBreakdown(BaseModel):
    total_ms: float
    classification_ms: float = 0.0
    retrieval_ms: float = 0.0
    grading_ms: float = 0.0
    generation_ms: float = 0.0
    cache_ms: float = 0.0


class QueryResponse(BaseModel):
    response: str
    citations: list[Citation]
    source: str = Field(..., description="'pipeline' or 'cache'")
    pipeline_trace: list[PipelineStep]
    timing: TimingBreakdown
    session_id: str
    grading_result: Optional[str] = None
    query_type: Optional[str] = None
