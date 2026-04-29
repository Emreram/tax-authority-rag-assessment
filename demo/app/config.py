from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # LLM — Docker Model Runner (OpenAI-compatible).
    llm_base_url: str = "http://model-runner.docker.internal:12434/engines/llama.cpp/v1"
    llm_model: str = "ai/gemma4:E2B"
    llm_timeout_s: int = 300  # Legacy global ceiling (HTTP client timeout)
    # Per-call timeouts — short for fast classify/grade, longer for streaming generate.
    # Each LLM helper accepts an explicit `timeout` arg; these are the call-site defaults.
    llm_timeout_classify_s: int = 15
    llm_timeout_grade_s: int = 15
    llm_timeout_hyde_s: int = 15
    llm_timeout_enrich_s: int = 30
    llm_timeout_generate_s: int = 60

    # Embedder (in-process sentence-transformers)
    embedding_model: str = "intfloat/multilingual-e5-small"
    embedding_dim: int = 384

    # OpenSearch
    opensearch_host: str = "opensearch"
    opensearch_port: int = 9200
    opensearch_index: str = "tax_authority_rag_chunks_e5"

    # Redis
    redis_host: str = "redis"
    redis_port: int = 6379
    cache_similarity_threshold: float = 0.97
    cache_ttl_default: int = 86400
    cache_ttl_procedural: int = 604800

    # Pipeline
    top_k_bm25: int = 10  # was 6 — wider candidate pool reduces false-refuses (grader has more to choose from)
    top_k_knn: int = 10   # was 6
    top_k_rerank: int = 5
    rrf_rank_constant: int = 60
    max_retries: int = 1
    min_relevant_chunks: int = 1

    # Conversation memory
    max_conversation_turns: int = 6

    # Feature flags — defaults tuned for live-demo on CPU.
    # HyDE is enabled by default (M4): adds one ~80-token LLM draft to lift
    # recall on terse SIMPLE queries. LLM-rerank stays off — overlapping cost
    # with the grader on a CPU-only stack.
    enable_hyde: bool = True
    enable_llm_rerank: bool = False

    class Config:
        env_file = ".env"
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
