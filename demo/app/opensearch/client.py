from opensearchpy import OpenSearch, RequestsHttpConnection
from app.config import get_settings
import structlog

log = structlog.get_logger()


def get_opensearch_client() -> OpenSearch:
    settings = get_settings()
    client = OpenSearch(
        hosts=[{"host": settings.opensearch_host, "port": settings.opensearch_port}],
        http_compress=True,
        use_ssl=False,
        verify_certs=False,
        connection_class=RequestsHttpConnection,
        timeout=30,
        max_retries=3,
        retry_on_timeout=True,
    )
    return client
