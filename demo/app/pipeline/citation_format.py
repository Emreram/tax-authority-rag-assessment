"""
Post-processing for LLM-generated answers:
  - Collapse verbose `[Source: chunk_id | hierarchy_path]` markers into compact `[N]` refs,
    deduplicated by chunk_id in order of first appearance.
  - Strip any 'Bronnen:' / 'Sources:' footer the model may have added — the UI already
    renders citation pills below the bubble, so the footer is redundant noise.

Returns the cleaned text and the ordered list of chunk_ids (so the UI can map [N] → pill).
"""

from __future__ import annotations

import re

# Matches [Source: chunk_id | hierarchy_path]  (path is optional)
_SOURCE_RE = re.compile(r"\[Source:\s*([^|\]\s][^|\]]*?)\s*(?:\|\s*[^\]]*)?\]", re.IGNORECASE)

# Matches a 'Bronnen:' / 'Sources:' footer at the end of the response, including any
# bullet/dash list items that follow. Tolerates **bold** wrapping and trailing text.
_BRONNEN_FOOTER_RE = re.compile(
    r"\n+\s*(?:\*\*|__)?\s*(?:Bronnen|Sources)\s*(?:\*\*|__)?\s*[:\.\-]?[^\n]*"
    r"(?:\n+(?:[-*]\s+|\d+\.\s+|\[?[A-Za-z0-9])[^\n]*)*\s*$",
    re.IGNORECASE,
)


def compact_citations(text: str, known_chunk_ids: set[str] | None = None) -> tuple[str, list[str]]:
    """
    Replace `[Source: cid | path]` markers with `[N]`, dedup by cid, strip Bronnen footer.

    Args:
        text: raw LLM output
        known_chunk_ids: if provided, unknown ids get their marker dropped instead of numbered
                         (they'll fall through to the implicit-citation fallback elsewhere)

    Returns:
        (cleaned_text, ordered_chunk_ids) — the index in the list + 1 == the [N] in cleaned_text
    """
    order: list[str] = []

    def _replace(match: re.Match) -> str:
        cid = match.group(1).strip().rstrip(",.;:")
        if known_chunk_ids is not None and cid not in known_chunk_ids:
            # tolerate small-model truncation: 'chunk009' often refers to a real id ending with it
            candidates = [k for k in known_chunk_ids if cid in k]
            if len(candidates) == 1:
                cid = candidates[0]
            else:
                return ""  # drop garbled marker
        if cid not in order:
            order.append(cid)
        return f"[{order.index(cid) + 1}]"

    cleaned = _SOURCE_RE.sub(_replace, text)
    cleaned = _BRONNEN_FOOTER_RE.sub("", cleaned)
    # collapse "  ." or "  ," that appear when a marker was dropped right before punctuation
    cleaned = re.sub(r"\s+([,.;:])", r"\1", cleaned)
    # collapse triple+ blank lines
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, order
