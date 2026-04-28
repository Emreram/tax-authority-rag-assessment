"""Deterministic chunk-id builder (ported from pseudocode/module1_ingestion.py:324-362)."""

from __future__ import annotations


def build_chunk_id(
    doc_id: str,
    chapter: str | None = None,
    section: str | None = None,
    article_num: str | None = None,
    paragraph_num: str | None = None,
    sub_paragraph: str | None = None,
    chunk_sequence: int = 0,
) -> str:
    parts: list[str] = [doc_id]
    if chapter:
        parts.append(f"ch{chapter}")
    if section:
        parts.append(f"sec{section}")
    if article_num:
        parts.append(f"art{article_num}")
    if paragraph_num:
        parts.append(f"par{paragraph_num}")
    if sub_paragraph:
        parts.append(f"sub{sub_paragraph}")
    parts.append(f"chunk{chunk_sequence:03d}")
    return "::".join(parts)


def build_hierarchy_path(
    doc_title: str,
    chapter: str | None = None,
    section: str | None = None,
    article_num: str | None = None,
    paragraph_num: str | None = None,
    sub_paragraph: str | None = None,
    consideration: str | None = None,
) -> str:
    first = doc_title.split()[0] if doc_title else "Doc"
    parts: list[str] = [first]
    if chapter:
        parts.append(f"Hoofdstuk {chapter}")
    if section:
        parts.append(f"Afdeling {section}")
    if article_num:
        parts.append(f"Art {article_num}")
    if paragraph_num:
        parts.append(f"Lid {paragraph_num}")
    if sub_paragraph:
        parts.append(f"Sub {sub_paragraph}")
    if consideration:
        parts.append(f"Overweging {consideration}")
    return " > ".join(parts)
