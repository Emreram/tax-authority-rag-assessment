"""
Structural chunker for Dutch legal documents.

Regex-based boundary detection for Hoofdstuk / Afdeling / Artikel / Lid / Sub.
Ported from pseudocode/module1_ingestion.py LegalStructureDetector.

The chunker is deterministic on legal text — we deliberately don't use an LLM
here because legal boundaries are strict formatting conventions (Artikel 3.114)
and regex is faster, auditable, and never hallucinates a boundary that isn't there.
For non-legal input we delegate to the semantic chunker (see semantic_chunker.py).
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# ── Dutch legislation patterns ──
CHAPTER_RE = re.compile(r"^(Hoofdstuk|HOOFDSTUK)\s+([A-Za-z0-9]+(?:[A-Za-z])?)\b", re.MULTILINE)
SECTION_RE = re.compile(r"^(Afdeling|AFDELING)\s+([\d]+(?:\.[\d]+)?)\b", re.MULTILINE)
ARTICLE_RE = re.compile(r"^(Artikel|Art\.?)\s+([\d]+(?:\.[\d]+)*[a-z]?)\b", re.MULTILINE)
PARAGRAPH_RE = re.compile(r"^(\d+)\.\s+", re.MULTILINE)
SUBPARA_RE = re.compile(r"^([a-z]|[ivx]+)[.°]\s+", re.MULTILINE)

# ── Case law ──
ECLI_RE = re.compile(r"ECLI:[A-Z]{2}:[A-Z]+:\d{4}:[A-Za-z0-9]+")
CONSIDERATION_RE = re.compile(r"^(?:r\.o\.\s*)?(\d+(?:\.\d+)?)\s+", re.MULTILINE)


@dataclass
class Boundary:
    level: str           # chapter | section | article | paragraph | sub_paragraph | consideration | document
    identifier: str
    label: str
    start: int
    end: int
    text: str


def has_structural_markers(text: str) -> bool:
    """True when regex markers indicate a document worth splitting structurally."""
    return any(r.search(text) for r in (CHAPTER_RE, ARTICLE_RE, CONSIDERATION_RE))


def classify_doc_type(text: str) -> str:
    if ECLI_RE.search(text) and CONSIDERATION_RE.search(text):
        return "CASE_LAW"
    if ARTICLE_RE.search(text) or CHAPTER_RE.search(text):
        return "LEGISLATION"
    return "UNSTRUCTURED"


def detect_legislation(text: str) -> list[Boundary]:
    raw: list[tuple[str, str, str, int]] = []
    for m in CHAPTER_RE.finditer(text):
        raw.append(("chapter", m.group(2), f"Hoofdstuk {m.group(2)}", m.start()))
    for m in SECTION_RE.finditer(text):
        raw.append(("section", m.group(2), f"Afdeling {m.group(2)}", m.start()))
    for m in ARTICLE_RE.finditer(text):
        raw.append(("article", m.group(2), f"Art {m.group(2)}", m.start()))
    for m in PARAGRAPH_RE.finditer(text):
        raw.append(("paragraph", m.group(1), f"Lid {m.group(1)}", m.start()))
    for m in SUBPARA_RE.finditer(text):
        raw.append(("sub_paragraph", m.group(1), f"Sub {m.group(1)}", m.start()))

    raw.sort(key=lambda x: x[3])
    out: list[Boundary] = []
    for i, (level, ident, label, start) in enumerate(raw):
        end = raw[i + 1][3] if i + 1 < len(raw) else len(text)
        body = text[start:end].strip()
        if not body:
            continue
        out.append(Boundary(level, ident, label, start, end, body))
    return out


def detect_case_law(text: str) -> list[Boundary]:
    raw: list[tuple[str, str, str, int]] = []
    for m in CONSIDERATION_RE.finditer(text):
        raw.append(("consideration", m.group(1), f"Overweging {m.group(1)}", m.start()))

    raw.sort(key=lambda x: x[3])
    out: list[Boundary] = []
    for i, (level, ident, label, start) in enumerate(raw):
        end = raw[i + 1][3] if i + 1 < len(raw) else len(text)
        body = text[start:end].strip()
        if not body:
            continue
        out.append(Boundary(level, ident, label, start, end, body))
    return out


def detect(text: str, doc_type: str | None = None) -> tuple[list[Boundary], str]:
    """
    Returns (boundaries, detected_type). detected_type ∈ LEGISLATION, CASE_LAW, UNSTRUCTURED.
    """
    resolved = doc_type or classify_doc_type(text)
    if resolved == "CASE_LAW":
        return detect_case_law(text), "CASE_LAW"
    if resolved == "LEGISLATION":
        return detect_legislation(text), "LEGISLATION"
    return [], "UNSTRUCTURED"
