"""
Gemini API wrapper.
Single model: configured via GEMINI_LLM_MODEL env var.
Embedding model: configured via GEMINI_EMBEDDING_MODEL env var.
"""

from google import genai
from google.genai import types
from app.config import get_settings
import structlog

log = structlog.get_logger()

_client: genai.Client | None = None


def get_client() -> genai.Client:
    global _client
    if _client is None:
        settings = get_settings()
        _client = genai.Client(api_key=settings.gemini_api_key)
    return _client


async def embed_text(text: str, task_type: str = "RETRIEVAL_QUERY") -> list[float]:
    """
    Embed text using text-embedding-004.
    task_type: RETRIEVAL_QUERY (queries) or RETRIEVAL_DOCUMENT (passages).
    """
    settings = get_settings()
    client = get_client()
    result = client.models.embed_content(
        model=settings.gemini_embedding_model,
        contents=text,
        config=types.EmbedContentConfig(task_type=task_type),
    )
    return result.embeddings[0].values


async def embed_document(text: str) -> list[float]:
    return await embed_text(text, task_type="RETRIEVAL_DOCUMENT")


async def generate(system_prompt: str, user_prompt: str, temperature: float = 0.0) -> str:
    """Call Gemini LLM and return the text response."""
    settings = get_settings()
    client = get_client()

    response = client.models.generate_content(
        model=settings.gemini_llm_model,
        contents=user_prompt,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=temperature,
            max_output_tokens=2048,
        ),
    )
    return response.text
