"""
Creates the OpenSearch index with the correct mapping and seeds it with
Dutch tax law chunks. Embeddings are generated at startup via Gemini.
"""

import json
import time
import asyncio
from pathlib import Path
from opensearchpy import OpenSearch
from opensearchpy.helpers import bulk
from app.config import get_settings
from app.opensearch.client import get_opensearch_client
from app.pipeline.llm import embed_document
import structlog

log = structlog.get_logger()

SEED_DATA_PATH = Path("/app/seed_data/chunks.json")


def wait_for_opensearch(client: OpenSearch, retries: int = 30, delay: int = 5) -> None:
    for i in range(retries):
        try:
            health = client.cluster.health(wait_for_status="yellow", timeout="10s")
            log.info("opensearch_ready", status=health["status"])
            return
        except Exception as e:
            log.info("opensearch_waiting", attempt=i + 1, error=str(e))
            time.sleep(delay)
    raise RuntimeError("OpenSearch did not become healthy in time")


def get_index_mapping(settings) -> dict:
    return {
        "settings": {
            "number_of_shards": 1,
            "number_of_replicas": 0,
            "analysis": {
                "filter": {
                    "dutch_stop": {"type": "stop", "stopwords": "_dutch_"},
                    "dutch_stemmer": {"type": "stemmer", "language": "dutch"},
                    "ascii_fold": {"type": "asciifolding"}
                },
                "analyzer": {
                    "dutch_legal_analyzer": {
                        "type": "custom",
                        "tokenizer": "standard",
                        "filter": ["lowercase", "ascii_fold", "dutch_stop", "dutch_stemmer"]
                    }
                }
            },
            "knn": True,
        },
        "mappings": {
            "properties": {
                "chunk_id": {"type": "keyword"},
                "doc_id": {"type": "keyword"},
                "doc_type": {"type": "keyword"},
                "title": {"type": "text", "analyzer": "dutch_legal_analyzer", "fields": {"keyword": {"type": "keyword"}}},
                "article_num": {"type": "keyword"},
                "paragraph_num": {"type": "keyword"},
                "chapter": {"type": "keyword"},
                "hierarchy_path": {"type": "text", "analyzer": "dutch_legal_analyzer"},
                "effective_date": {"type": "date"},
                "expiry_date": {"type": "date", "null_value": None},
                "version": {"type": "integer"},
                "security_classification": {"type": "keyword"},
                "language": {"type": "keyword"},
                "ecli_id": {"type": "keyword"},
                "chunk_sequence": {"type": "integer"},
                "token_count": {"type": "integer"},
                "ingestion_timestamp": {"type": "date"},
                "source_url": {"type": "keyword"},
                "parent_chunk_id": {"type": "keyword"},
                "chunk_text": {
                    "type": "text",
                    "analyzer": "dutch_legal_analyzer",
                    "fields": {"keyword": {"type": "keyword", "ignore_above": 256}}
                },
                "embedding": {
                    "type": "knn_vector",
                    "dimension": settings.embedding_dim,
                    "method": {
                        "name": "hnsw",
                        "space_type": "cosinesimil",
                        "engine": "nmslib",
                        "parameters": {"m": 16, "ef_construction": 128}
                    }
                }
            }
        }
    }


async def create_index(client: OpenSearch, settings) -> None:
    index = settings.opensearch_index
    if client.indices.exists(index=index):
        log.info("index_exists", index=index)
        count = client.count(index=index)["count"]
        if count > 0:
            log.info("index_has_data", count=count)
            return
        log.info("index_empty_reseeding")
    else:
        client.indices.create(index=index, body=get_index_mapping(settings))
        log.info("index_created", index=index)

    await seed_data(client, settings)


async def seed_data(client: OpenSearch, settings) -> None:
    log.info("seeding_started")

    with open(SEED_DATA_PATH) as f:
        chunks = json.load(f)

    actions = []
    for i, chunk in enumerate(chunks):
        log.info("embedding_chunk", i=i + 1, total=len(chunks), chunk_id=chunk["chunk_id"])
        embedding = await embed_document(chunk["chunk_text"])

        doc = {k: v for k, v in chunk.items()}
        doc["embedding"] = embedding

        actions.append({
            "_index": settings.opensearch_index,
            "_id": chunk["chunk_id"],
            "_source": doc,
        })

    success, errors = bulk(client, actions, refresh=True)
    log.info("seeding_complete", indexed=success, errors=len(errors) if errors else 0)


async def setup_opensearch() -> None:
    settings = get_settings()
    client = get_opensearch_client()
    wait_for_opensearch(client)
    await create_index(client, settings)
