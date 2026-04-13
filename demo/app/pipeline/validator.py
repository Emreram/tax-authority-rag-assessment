"""
Citation validator — verifies every cited chunk_id exists in the graded context.
Prevents hallucinated citations from reaching the user.
"""

import structlog

log = structlog.get_logger()


def validate_citations(response_text: str, cited_ids: list[str], graded_chunks: list[dict]) -> dict:
    """
    Returns {valid: bool, reason: str, fabricated: list[str]}
    """
    if not cited_ids:
        return {"valid": False, "reason": "No citations found in response", "fabricated": []}

    available_ids = {c["chunk_id"] for c in graded_chunks}
    fabricated = [cid for cid in cited_ids if cid not in available_ids]

    if fabricated:
        log.warning("fabricated_citations", fabricated=fabricated)
        return {
            "valid": False,
            "reason": f"Fabricated citation(s) detected: {fabricated}",
            "fabricated": fabricated,
        }

    log.info("citation_validation_passed", cited=len(cited_ids))
    return {"valid": True, "reason": "All citations verified", "fabricated": []}
