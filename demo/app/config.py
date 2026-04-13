from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # Gemini
    gemini_api_key: str
    gemini_llm_model: str = "gemini-3.1-pro-preview"
    gemini_embedding_model: str = "gemini-embedding-001"

    # OpenSearch
    opensearch_host: str = "opensearch"
    opensearch_port: int = 9200
    opensearch_index: str = "tax_authority_rag_chunks"
    embedding_dim: int = 3072  # gemini-embedding-001 output dimension

    # Redis
    redis_host: str = "redis"
    redis_port: int = 6379
    cache_similarity_threshold: float = 0.97
    cache_ttl_default: int = 86400     # 24 hours
    cache_ttl_procedural: int = 604800  # 7 days

    # Pipeline
    top_k_bm25: int = 10
    top_k_knn: int = 10
    top_k_rerank: int = 8
    rrf_rank_constant: int = 60
    max_retries: int = 1
    min_relevant_chunks: int = 1

    class Config:
        env_file = ".env"
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
