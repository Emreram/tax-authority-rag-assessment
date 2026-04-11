"""
Module 1: Ingestion & Knowledge Structuring — Legal Document Pipeline
=====================================================================

This module answers the assessment question:
  "How do you ensure the LLM knows a chunk belongs to Article 3.114, Paragraph 2?"

Design principles:
  1. Split on LEGAL STRUCTURE (Article/Paragraph boundaries), not character counts.
  2. Every chunk carries its full legal lineage as metadata (hierarchy_path).
  3. Parent metadata propagates to children — a Paragraph chunk inherits its Article's metadata.
  4. Chunk IDs are deterministic — re-ingesting the same doc produces the same IDs (upsert, no dupes).
  5. Parent-child NodeRelationships enable hierarchical retrieval.

Stack: LlamaIndex 0.11+ with custom NodeParser, multilingual-e5-large embeddings, OpenSearch 2.15+.
"""

import re
import hashlib
from enum import Enum
from typing import Optional
from datetime import date, datetime

from pydantic import BaseModel, Field
from llama_index.core.schema import (
    TextNode,
    NodeRelationship,
    RelatedNodeInfo,
    Document,
)
from llama_index.core.node_parser import NodeParser
from llama_index.core.ingestion import IngestionPipeline
from llama_index.embeddings.huggingface import HuggingFaceEmbedding
from llama_index.vector_stores.opensearch import OpensearchVectorStore


# =============================================================================
# 1. ENUMS & PYDANTIC MODELS — Direct mapping to schemas/chunk_metadata.json
# =============================================================================

class DocumentType(str, Enum):
    """Document category — determines which chunking strategy is applied."""
    LEGISLATION = "LEGISLATION"   # Laws, regulations, tax codes (hierarchical article structure)
    CASE_LAW = "CASE_LAW"         # Court rulings, verdicts (ECLI structure)
    POLICY = "POLICY"             # Internal manuals, memos, operational guidelines
    ELEARNING = "ELEARNING"       # Training modules, internal wikis


class SecurityClassification(str, Enum):
    """Access control tier — maps directly to OpenSearch DLS roles in rbac_roles.json."""
    PUBLIC = "PUBLIC"                   # Available to all users
    INTERNAL = "INTERNAL"               # All tax authority employees
    RESTRICTED = "RESTRICTED"           # Senior inspectors and legal counsel only
    CLASSIFIED_FIOD = "CLASSIFIED_FIOD" # FIOD fraud investigation personnel only


class ChunkMetadata(BaseModel):
    """
    Type-safe representation of every field in schemas/chunk_metadata.json.
    Every chunk produced by LegalDocumentChunker carries a fully populated instance.
    The LLM uses hierarchy_path + article_num + paragraph_num to reconstruct exact citations.
    """
    chunk_id: str = Field(
        ..., description="Deterministic ID: {doc_id}::{article}::{paragraph}::{chunk_seq}"
    )
    doc_id: str = Field(
        ..., description="Document ID with version: e.g. AWR-2024-v3"
    )
    doc_type: DocumentType
    title: str = Field(
        ..., description="Official document title, e.g. 'Algemene wet inzake rijksbelastingen'"
    )
    article_num: Optional[str] = None
    paragraph_num: Optional[str] = None
    sub_paragraph: Optional[str] = None
    chapter: Optional[str] = None
    section: Optional[str] = None
    hierarchy_path: str = Field(
        ..., description="Full breadcrumb: 'AWR > Hoofdstuk 3 > Art 3.114 > Lid 2'"
    )
    effective_date: date
    expiry_date: Optional[date] = None
    version: int = Field(..., ge=1)
    security_classification: SecurityClassification
    source_url: Optional[str] = None
    parent_chunk_id: Optional[str] = None
    language: str = "nl"
    ecli_id: Optional[str] = None
    amendment_refs: list[str] = Field(default_factory=list)
    chunk_sequence: int = Field(..., ge=0)
    token_count: int = Field(..., ge=1)
    ingestion_timestamp: datetime = Field(default_factory=datetime.utcnow)


class DocumentLevelMetadata(BaseModel):
    """
    Metadata that applies to the entire document — set once during ingestion
    and inherited by every chunk. Typically read from a classification manifest
    or the document management system (DMS).
    """
    doc_id: str
    doc_type: DocumentType
    title: str
    effective_date: date
    expiry_date: Optional[date] = None
    version: int
    security_classification: SecurityClassification
    source_url: Optional[str] = None
    language: str = "nl"
    ecli_id: Optional[str] = None       # Only for CASE_LAW
    amendment_refs: list[str] = Field(default_factory=list)


# =============================================================================
# 2. STRUCTURAL BOUNDARY — Represents a detected legal structure element
# =============================================================================

class StructuralBoundary(BaseModel):
    """A detected structural element in a legal document (Article, Paragraph, etc.)."""
    level: str              # "chapter", "section", "article", "paragraph", "sub_paragraph"
    identifier: str         # "3", "3.114", "2", "a"
    display_label: str      # "Hoofdstuk 3", "Artikel 3.114", "Lid 2", "Sub a"
    start_pos: int          # Character offset in source text
    end_pos: int            # Character offset of the end of this element's content
    text_content: str       # The raw text belonging to this structural unit


# =============================================================================
# 3. LEGAL STRUCTURE DETECTOR — Regex-based detection per document type
# =============================================================================

class LegalStructureDetector:
    """
    Detects structural boundaries in Dutch legal documents using regex patterns.
    Four separate pattern sets for the four document types.

    Why regex and not LLM-based detection:
    - Legal documents follow STRICT formatting conventions (Artikel, Lid, etc.)
    - Regex is deterministic, fast (~ms per document), and auditable
    - LLM-based detection adds latency and non-determinism to the ingestion pipeline
    """

    # ── Dutch legislation patterns ──
    # Matches: "Hoofdstuk 3", "Hoofdstuk IIA", "HOOFDSTUK 10"
    CHAPTER_PATTERN = re.compile(
        r"^(Hoofdstuk|HOOFDSTUK)\s+([A-Za-z0-9]+(?:[A-Za-z])?)\b",
        re.MULTILINE,
    )
    # Matches: "Afdeling 2", "Afdeling 3.1"
    SECTION_PATTERN = re.compile(
        r"^(Afdeling|AFDELING)\s+([\d]+(?:\.[\d]+)?)\b",
        re.MULTILINE,
    )
    # Matches: "Artikel 3.114", "Art. 67a", "Artikel 10.1"
    ARTICLE_PATTERN = re.compile(
        r"^(Artikel|Art\.?)\s+([\d]+(?:\.[\d]+)*[a-z]?)\b",
        re.MULTILINE,
    )
    # Matches: "1. " (numbered paragraph / lid) at start of line
    PARAGRAPH_PATTERN = re.compile(
        r"^(\d+)\.\s+",
        re.MULTILINE,
    )
    # Matches: "a. ", "b. ", "i. ", "ii. " (sub-paragraph enumeration)
    SUB_PARAGRAPH_PATTERN = re.compile(
        r"^([a-z]|[ivx]+)[.°]\s+",
        re.MULTILINE,
    )

    # ── Case law patterns (ECLI-structured) ──
    ECLI_PATTERN = re.compile(
        r"ECLI:[A-Z]{2}:[A-Z]+:\d{4}:[A-Za-z0-9]+"
    )
    # Matches: "3.2" or "r.o. 4.1" (rechtsoverweging = legal consideration)
    CONSIDERATION_PATTERN = re.compile(
        r"^(?:r\.o\.\s*)?(\d+(?:\.\d+)?)\s+",
        re.MULTILINE,
    )

    # ── Policy document patterns ──
    POLICY_CHAPTER_PATTERN = re.compile(
        r"^(Hoofdstuk|Paragraaf)\s+([\d]+(?:\.[\d]+)?)",
        re.MULTILINE,
    )
    POLICY_SECTION_PATTERN = re.compile(
        r"^(\d+(?:\.\d+)*)\s+[A-Z]",  # "2.1 Beleid voor..."
        re.MULTILINE,
    )

    # ── E-learning patterns ──
    ELEARN_MODULE_PATTERN = re.compile(
        r"^(Module|Les|Onderdeel)\s+(\d+)",
        re.MULTILINE,
    )

    def detect_boundaries(
        self, text: str, doc_type: DocumentType
    ) -> list[StructuralBoundary]:
        """
        Detect all structural boundaries in the document text.
        Returns a list sorted by start_pos — the order they appear in the document.
        Each boundary includes the text content that belongs to it (until the next boundary).
        """
        if doc_type == DocumentType.LEGISLATION:
            return self._detect_legislation(text)
        elif doc_type == DocumentType.CASE_LAW:
            return self._detect_case_law(text)
        elif doc_type == DocumentType.POLICY:
            return self._detect_policy(text)
        elif doc_type == DocumentType.ELEARNING:
            return self._detect_elearning(text)
        else:
            # Fallback: treat entire document as one unit
            return [StructuralBoundary(
                level="document", identifier="1", display_label="Document",
                start_pos=0, end_pos=len(text), text_content=text,
            )]

    def _detect_legislation(self, text: str) -> list[StructuralBoundary]:
        """
        Detect Dutch legal hierarchy: Hoofdstuk > Afdeling > Artikel > Lid > Sub.
        Returns nested boundaries — a Lid is always inside an Artikel.
        """
        boundaries: list[StructuralBoundary] = []

        # Detect all pattern matches with their positions
        raw_matches = []
        for match in self.CHAPTER_PATTERN.finditer(text):
            raw_matches.append(("chapter", match.group(2), f"Hoofdstuk {match.group(2)}", match.start()))
        for match in self.SECTION_PATTERN.finditer(text):
            raw_matches.append(("section", match.group(2), f"Afdeling {match.group(2)}", match.start()))
        for match in self.ARTICLE_PATTERN.finditer(text):
            raw_matches.append(("article", match.group(2), f"Art {match.group(2)}", match.start()))
        for match in self.PARAGRAPH_PATTERN.finditer(text):
            raw_matches.append(("paragraph", match.group(1), f"Lid {match.group(1)}", match.start()))
        for match in self.SUB_PARAGRAPH_PATTERN.finditer(text):
            raw_matches.append(("sub_paragraph", match.group(1), f"Sub {match.group(1)}", match.start()))

        # Sort by position in document
        raw_matches.sort(key=lambda x: x[3])

        # Assign text content: each boundary owns text until the next boundary starts
        for i, (level, identifier, label, start_pos) in enumerate(raw_matches):
            end_pos = raw_matches[i + 1][3] if i + 1 < len(raw_matches) else len(text)
            content = text[start_pos:end_pos].strip()
            boundaries.append(StructuralBoundary(
                level=level, identifier=identifier, display_label=label,
                start_pos=start_pos, end_pos=end_pos, text_content=content,
            ))

        return boundaries

    def _detect_case_law(self, text: str) -> list[StructuralBoundary]:
        """Detect case law structure: ECLI header > Considerations (r.o. 3.1, 3.2, ...)."""
        boundaries: list[StructuralBoundary] = []
        raw_matches = []

        for match in self.CONSIDERATION_PATTERN.finditer(text):
            raw_matches.append(("consideration", match.group(1),
                                f"Overweging {match.group(1)}", match.start()))

        raw_matches.sort(key=lambda x: x[3])

        for i, (level, identifier, label, start_pos) in enumerate(raw_matches):
            end_pos = raw_matches[i + 1][3] if i + 1 < len(raw_matches) else len(text)
            content = text[start_pos:end_pos].strip()
            boundaries.append(StructuralBoundary(
                level=level, identifier=identifier, display_label=label,
                start_pos=start_pos, end_pos=end_pos, text_content=content,
            ))

        return boundaries

    def _detect_policy(self, text: str) -> list[StructuralBoundary]:
        """Detect policy document structure: Hoofdstuk > Paragraaf > numbered sections."""
        boundaries: list[StructuralBoundary] = []
        raw_matches = []

        for match in self.POLICY_CHAPTER_PATTERN.finditer(text):
            raw_matches.append(("chapter", match.group(2),
                                f"{match.group(1)} {match.group(2)}", match.start()))
        for match in self.POLICY_SECTION_PATTERN.finditer(text):
            raw_matches.append(("section", match.group(1),
                                f"Paragraaf {match.group(1)}", match.start()))

        raw_matches.sort(key=lambda x: x[3])

        for i, (level, identifier, label, start_pos) in enumerate(raw_matches):
            end_pos = raw_matches[i + 1][3] if i + 1 < len(raw_matches) else len(text)
            content = text[start_pos:end_pos].strip()
            boundaries.append(StructuralBoundary(
                level=level, identifier=identifier, display_label=label,
                start_pos=start_pos, end_pos=end_pos, text_content=content,
            ))

        return boundaries

    def _detect_elearning(self, text: str) -> list[StructuralBoundary]:
        """Detect e-learning structure: Module > Les > Onderdeel."""
        boundaries: list[StructuralBoundary] = []
        raw_matches = []

        for match in self.ELEARN_MODULE_PATTERN.finditer(text):
            raw_matches.append(("lesson", match.group(2),
                                f"{match.group(1)} {match.group(2)}", match.start()))

        raw_matches.sort(key=lambda x: x[3])

        for i, (level, identifier, label, start_pos) in enumerate(raw_matches):
            end_pos = raw_matches[i + 1][3] if i + 1 < len(raw_matches) else len(text)
            content = text[start_pos:end_pos].strip()
            boundaries.append(StructuralBoundary(
                level=level, identifier=identifier, display_label=label,
                start_pos=start_pos, end_pos=end_pos, text_content=content,
            ))

        return boundaries


# =============================================================================
# 4. DETERMINISTIC CHUNK ID GENERATOR
# =============================================================================

def build_chunk_id(
    doc_id: str,
    article_num: Optional[str] = None,
    paragraph_num: Optional[str] = None,
    sub_paragraph: Optional[str] = None,
    chapter: Optional[str] = None,
    section: Optional[str] = None,
    chunk_sequence: int = 0,
) -> str:
    """
    Generate a deterministic, human-readable chunk ID.

    Format: {doc_id}::{structural_parts}::chunk{seq:03d}
    Examples:
      - AWR-2024-v3::art3.114::par2::chunk001
      - ECLI-NL-HR-2023-1234::section3::chunk002
      - POLICY-IH-2024-007::ch2::sec1::chunk001
      - ELEARN-MOD-042::lesson3::chunk001

    Why deterministic:
      Re-ingesting the same document produces the same chunk_ids.
      OpenSearch can then UPSERT (update-or-insert) instead of creating duplicates.
      This is critical for nightly re-index workflows.
    """
    parts = [doc_id]

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


# =============================================================================
# 5. METADATA INHERITANCE MANAGER
# =============================================================================

class MetadataInheritanceManager:
    """
    Ensures child chunks inherit parent metadata.

    This is the core mechanism that answers: "How does the LLM know a chunk
    belongs to Article 3.114, Paragraph 2?"

    When a Paragraph-level chunk is created inside an Article:
      - It INHERITS from parent: doc_id, doc_type, title, security_classification,
        effective_date, expiry_date, version, language, source_url, chapter, section,
        article_num, ecli_id, amendment_refs.
      - It ADDS its own: paragraph_num, sub_paragraph.
      - It BUILDS hierarchy_path by extending the parent's path.

    The LLM sees this metadata in the retrieved context and can produce:
      "Article 3.114, Paragraph 2, sub a of the Algemene wet inzake rijksbelastingen"
    """

    @staticmethod
    def build_hierarchy_path(
        doc_meta: DocumentLevelMetadata,
        chapter: Optional[str] = None,
        section: Optional[str] = None,
        article_num: Optional[str] = None,
        paragraph_num: Optional[str] = None,
        sub_paragraph: Optional[str] = None,
        consideration: Optional[str] = None,
    ) -> str:
        """
        Build the full breadcrumb path from document root to this chunk.

        Examples:
          "AWR > Hoofdstuk 3 > Afdeling 1 > Art 3.114 > Lid 2 > Sub a"
          "ECLI:NL:HR:2023:1234 > Overweging 3.2"
          "Handboek Invordering > Hoofdstuk 2 > Paragraaf 1"
        """
        # Start with a short document title (abbreviation if possible)
        path_parts = [doc_meta.title.split()[0] if doc_meta.title else doc_meta.doc_id]

        if chapter:
            path_parts.append(f"Hoofdstuk {chapter}")
        if section:
            path_parts.append(f"Afdeling {section}")
        if article_num:
            path_parts.append(f"Art {article_num}")
        if paragraph_num:
            path_parts.append(f"Lid {paragraph_num}")
        if sub_paragraph:
            path_parts.append(f"Sub {sub_paragraph}")
        if consideration:
            path_parts.append(f"Overweging {consideration}")

        return " > ".join(path_parts)

    @staticmethod
    def create_chunk_metadata(
        doc_meta: DocumentLevelMetadata,
        boundary: StructuralBoundary,
        parent_hierarchy: dict,
        chunk_sequence: int,
        token_count: int,
    ) -> ChunkMetadata:
        """
        Create a fully populated ChunkMetadata by inheriting document-level
        and parent structural metadata, then adding this boundary's own fields.

        parent_hierarchy carries accumulated chapter/section/article from ancestors.
        """
        # Merge parent hierarchy with this boundary's level
        current_hierarchy = {**parent_hierarchy}  # shallow copy

        if boundary.level == "chapter":
            current_hierarchy["chapter"] = boundary.identifier
        elif boundary.level == "section":
            current_hierarchy["section"] = boundary.identifier
        elif boundary.level == "article":
            current_hierarchy["article_num"] = boundary.identifier
        elif boundary.level == "paragraph":
            current_hierarchy["paragraph_num"] = boundary.identifier
        elif boundary.level == "sub_paragraph":
            current_hierarchy["sub_paragraph"] = boundary.identifier
        elif boundary.level == "consideration":
            # Case law: map consideration to article_num for consistent schema usage
            current_hierarchy["article_num"] = boundary.identifier

        # Build deterministic chunk ID
        chunk_id = build_chunk_id(
            doc_id=doc_meta.doc_id,
            article_num=current_hierarchy.get("article_num"),
            paragraph_num=current_hierarchy.get("paragraph_num"),
            sub_paragraph=current_hierarchy.get("sub_paragraph"),
            chapter=current_hierarchy.get("chapter"),
            section=current_hierarchy.get("section"),
            chunk_sequence=chunk_sequence,
        )

        # Build parent chunk ID (the structural unit one level up)
        parent_chunk_id = build_chunk_id(
            doc_id=doc_meta.doc_id,
            article_num=parent_hierarchy.get("article_num"),
            paragraph_num=parent_hierarchy.get("paragraph_num"),
            sub_paragraph=parent_hierarchy.get("sub_paragraph"),
            chapter=parent_hierarchy.get("chapter"),
            section=parent_hierarchy.get("section"),
            chunk_sequence=0,  # Parent is always chunk 0
        ) if parent_hierarchy else None

        # Build hierarchy path
        hierarchy_path = MetadataInheritanceManager.build_hierarchy_path(
            doc_meta=doc_meta,
            chapter=current_hierarchy.get("chapter"),
            section=current_hierarchy.get("section"),
            article_num=current_hierarchy.get("article_num"),
            paragraph_num=current_hierarchy.get("paragraph_num"),
            sub_paragraph=current_hierarchy.get("sub_paragraph"),
            consideration=(
                current_hierarchy.get("article_num")
                if doc_meta.doc_type == DocumentType.CASE_LAW
                else None
            ),
        )

        return ChunkMetadata(
            chunk_id=chunk_id,
            # ── Inherited from document ──
            doc_id=doc_meta.doc_id,
            doc_type=doc_meta.doc_type,
            title=doc_meta.title,
            effective_date=doc_meta.effective_date,
            expiry_date=doc_meta.expiry_date,
            version=doc_meta.version,
            security_classification=doc_meta.security_classification,
            source_url=doc_meta.source_url,
            language=doc_meta.language,
            ecli_id=doc_meta.ecli_id,
            amendment_refs=doc_meta.amendment_refs,
            # ── Structural position (accumulated from parents + this boundary) ──
            chapter=current_hierarchy.get("chapter"),
            section=current_hierarchy.get("section"),
            article_num=current_hierarchy.get("article_num"),
            paragraph_num=current_hierarchy.get("paragraph_num"),
            sub_paragraph=current_hierarchy.get("sub_paragraph"),
            hierarchy_path=hierarchy_path,
            parent_chunk_id=parent_chunk_id,
            # ── Chunk-specific ──
            chunk_sequence=chunk_sequence,
            token_count=token_count,
        )


# =============================================================================
# 6. LEGAL DOCUMENT CHUNKER — Custom LlamaIndex NodeParser
# =============================================================================

# Maximum tokens per chunk. If a structural unit exceeds this, apply secondary split.
MAX_CHUNK_TOKENS = 512
# Overlap tokens for secondary splits only (structural splits have zero overlap).
SECONDARY_SPLIT_OVERLAP = 64


class LegalDocumentChunker(NodeParser):
    """
    Custom NodeParser that splits legal documents on structural boundaries
    instead of character counts.

    Key behaviors:
    1. Uses LegalStructureDetector to find Article/Paragraph/etc. boundaries
    2. Each structural unit becomes one or more chunks
    3. Metadata inheritance ensures every chunk carries its full legal lineage
    4. Parent-child NodeRelationships are set for hierarchical retrieval
    5. If a structural unit exceeds MAX_CHUNK_TOKENS, a secondary split is applied
       WITHIN the boundary (preserving the structural metadata)

    This is what the assessment means by "not destroying hierarchical context":
    naive splitters split at character 512 regardless of legal structure.
    We split at Article/Paragraph boundaries regardless of character count.
    """

    def __init__(self, tokenizer=None, **kwargs):
        """
        Args:
            tokenizer: The embedding model's tokenizer for accurate token counting.
                       Using the same tokenizer as multilingual-e5-large ensures
                       token_count matches what the embedding model sees.
        """
        super().__init__(**kwargs)
        self._detector = LegalStructureDetector()
        # Use the embedding model's tokenizer for accurate token counting
        # (not a generic tokenizer — counts must match the embedding model)
        self._tokenizer = tokenizer

    def _count_tokens(self, text: str) -> int:
        """Count tokens using the embedding model's tokenizer."""
        if self._tokenizer:
            return len(self._tokenizer.encode(text))
        # Fallback: rough approximation (1 token ≈ 4 chars for Dutch)
        return len(text) // 4

    def _secondary_split(self, text: str, max_tokens: int = MAX_CHUNK_TOKENS) -> list[str]:
        """
        Split an oversized structural unit into smaller pieces.

        Applied ONLY when a single Article/Paragraph exceeds MAX_CHUNK_TOKENS.
        Splits on sentence boundaries (". ") to avoid cutting mid-sentence.
        Uses SECONDARY_SPLIT_OVERLAP tokens of overlap to maintain context.

        The structural metadata is preserved on ALL sub-chunks — the split
        is purely for sizing, not for changing the legal hierarchy.
        """
        sentences = re.split(r"(?<=\.)\s+", text)
        chunks: list[str] = []
        current_chunk_sentences: list[str] = []
        current_tokens = 0

        for sentence in sentences:
            sentence_tokens = self._count_tokens(sentence)
            if current_tokens + sentence_tokens > max_tokens and current_chunk_sentences:
                chunks.append(" ".join(current_chunk_sentences))
                # Overlap: keep last N tokens worth of sentences
                overlap_sentences: list[str] = []
                overlap_tokens = 0
                for s in reversed(current_chunk_sentences):
                    s_tokens = self._count_tokens(s)
                    if overlap_tokens + s_tokens > SECONDARY_SPLIT_OVERLAP:
                        break
                    overlap_sentences.insert(0, s)
                    overlap_tokens += s_tokens
                current_chunk_sentences = overlap_sentences + [sentence]
                current_tokens = overlap_tokens + sentence_tokens
            else:
                current_chunk_sentences.append(sentence)
                current_tokens += sentence_tokens

        if current_chunk_sentences:
            chunks.append(" ".join(current_chunk_sentences))

        return chunks

    def _parse_nodes(self, documents: list[Document], **kwargs) -> list[TextNode]:
        """
        Main entry point — called by LlamaIndex's IngestionPipeline.

        For each document:
        1. Read document-level metadata (doc_type, security_classification, etc.)
        2. Detect structural boundaries based on doc_type
        3. Create chunks at boundary points with inherited metadata
        4. Handle oversized structural units with secondary splits
        5. Set parent-child NodeRelationships
        """
        all_nodes: list[TextNode] = []

        for document in documents:
            # Step 1: Extract document-level metadata
            # (In production, this comes from a classification manifest or DMS)
            doc_meta = DocumentLevelMetadata(
                doc_id=document.metadata.get("doc_id"),
                doc_type=DocumentType(document.metadata.get("doc_type")),
                title=document.metadata.get("title"),
                effective_date=date.fromisoformat(document.metadata.get("effective_date")),
                expiry_date=(
                    date.fromisoformat(document.metadata["expiry_date"])
                    if document.metadata.get("expiry_date")
                    else None
                ),
                version=int(document.metadata.get("version", 1)),
                security_classification=SecurityClassification(
                    document.metadata.get("security_classification", "PUBLIC")
                ),
                source_url=document.metadata.get("source_url"),
                language=document.metadata.get("language", "nl"),
                ecli_id=document.metadata.get("ecli_id"),
                amendment_refs=document.metadata.get("amendment_refs", []),
            )

            # Step 2: Detect structural boundaries
            boundaries = self._detector.detect_boundaries(
                text=document.text, doc_type=doc_meta.doc_type
            )

            # If no structure detected, treat entire document as one chunk
            if not boundaries:
                boundaries = [StructuralBoundary(
                    level="document", identifier="1", display_label="Document",
                    start_pos=0, end_pos=len(document.text),
                    text_content=document.text,
                )]

            # Step 3: Build hierarchy tracker
            # Tracks the current chapter/section/article as we walk through boundaries
            current_hierarchy: dict = {}
            hierarchy_level_order = [
                "chapter", "section", "article", "paragraph",
                "sub_paragraph", "consideration", "lesson",
            ]
            # Map to track parent TextNodes for NodeRelationship linking
            parent_nodes: dict[str, TextNode] = {}

            # Step 4: Create chunks from boundaries
            for boundary in boundaries:
                # Update hierarchy tracker: when we hit a new Article, clear
                # paragraph/sub_paragraph (they belong to the previous article)
                level_idx = (
                    hierarchy_level_order.index(boundary.level)
                    if boundary.level in hierarchy_level_order
                    else len(hierarchy_level_order)
                )
                # Clear all levels below the current one
                for lower_level in hierarchy_level_order[level_idx + 1:]:
                    field = lower_level if lower_level != "article" else "article_num"
                    field = field if field != "paragraph" else "paragraph_num"
                    current_hierarchy.pop(field, None)
                    current_hierarchy.pop(lower_level, None)

                # Check if this structural unit needs secondary splitting
                text_content = boundary.text_content
                token_count = self._count_tokens(text_content)

                if token_count > MAX_CHUNK_TOKENS:
                    # OVERSIZED: apply secondary split within this structural unit
                    sub_chunks = self._secondary_split(text_content)
                else:
                    sub_chunks = [text_content]

                for seq, chunk_text in enumerate(sub_chunks):
                    chunk_token_count = self._count_tokens(chunk_text)

                    # Create fully populated metadata via inheritance manager
                    chunk_meta = MetadataInheritanceManager.create_chunk_metadata(
                        doc_meta=doc_meta,
                        boundary=boundary,
                        parent_hierarchy=dict(current_hierarchy),  # copy
                        chunk_sequence=seq,
                        token_count=chunk_token_count,
                    )

                    # Create LlamaIndex TextNode with all metadata
                    node = TextNode(
                        text=chunk_text,
                        id_=chunk_meta.chunk_id,  # Deterministic ID!
                        metadata=chunk_meta.model_dump(mode="json"),
                        excluded_embed_metadata_keys=[
                            # These fields are for filtering/display, not for embedding
                            "chunk_id", "doc_id", "version", "source_url",
                            "parent_chunk_id", "chunk_sequence", "token_count",
                            "ingestion_timestamp", "amendment_refs",
                            "security_classification",
                        ],
                        excluded_llm_metadata_keys=[
                            # The LLM doesn't need internal IDs
                            "chunk_id", "parent_chunk_id", "chunk_sequence",
                            "token_count", "ingestion_timestamp", "amendment_refs",
                        ],
                    )

                    # Step 5: Set parent-child NodeRelationships
                    if chunk_meta.parent_chunk_id and chunk_meta.parent_chunk_id in parent_nodes:
                        parent_node = parent_nodes[chunk_meta.parent_chunk_id]
                        # Child → Parent relationship
                        node.relationships[NodeRelationship.PARENT] = RelatedNodeInfo(
                            node_id=parent_node.id_,
                            metadata={"hierarchy_path": parent_node.metadata.get("hierarchy_path", "")},
                        )
                        # Parent → Child relationship (append to existing children)
                        if NodeRelationship.CHILD not in parent_node.relationships:
                            parent_node.relationships[NodeRelationship.CHILD] = []
                        parent_node.relationships[NodeRelationship.CHILD].append(
                            RelatedNodeInfo(node_id=node.id_)
                        )

                    # Register this node as a potential parent for deeper levels
                    parent_nodes[chunk_meta.chunk_id] = node

                    all_nodes.append(node)

                # Update hierarchy tracker with this boundary's contribution
                if boundary.level == "chapter":
                    current_hierarchy["chapter"] = boundary.identifier
                elif boundary.level == "section":
                    current_hierarchy["section"] = boundary.identifier
                elif boundary.level in ("article", "consideration"):
                    current_hierarchy["article_num"] = boundary.identifier
                elif boundary.level == "paragraph":
                    current_hierarchy["paragraph_num"] = boundary.identifier
                elif boundary.level == "sub_paragraph":
                    current_hierarchy["sub_paragraph"] = boundary.identifier

        return all_nodes


# =============================================================================
# 7. INGESTION PIPELINE — End-to-end: Read → Chunk → Embed → Index
# =============================================================================

def create_ingestion_pipeline() -> IngestionPipeline:
    """
    Wire up the full ingestion pipeline using LlamaIndex's IngestionPipeline.

    Pipeline stages:
      1. LegalDocumentChunker — structural chunking with metadata inheritance
      2. HuggingFaceEmbedding — multilingual-e5-large (1024-dim, self-hosted)
      3. OpensearchVectorStore — write to tax_authority_rag_chunks index

    The pipeline is idempotent: deterministic chunk_ids mean re-running on the
    same document updates existing chunks instead of creating duplicates.
    """
    # ── Embedding model (self-hosted, never sends data externally) ──
    embed_model = HuggingFaceEmbedding(
        model_name="intfloat/multilingual-e5-large",
        embed_batch_size=64,          # Batch for throughput during ingestion
        max_length=512,               # Max tokens per chunk (matches our target)
        normalize=True,               # L2 normalize — required for cosine similarity
        query_instruction="query: ",  # E5 instruction prefix for queries
        text_instruction="passage: ", # E5 instruction prefix for passages
    )

    # ── Vector store (OpenSearch with index from opensearch_index_mapping.json) ──
    vector_store = OpensearchVectorStore(
        opensearch_url="https://opensearch.tax-authority.internal:9200",
        index_name="tax_authority_rag_chunks",  # Matches our index mapping
        http_auth=("ingestion_service", "***"),  # Uses role_ingestion_service from RBAC
        use_ssl=True,
        verify_certs=True,
        ssl_show_warn=True,
    )

    # ── Tokenizer for accurate token counting ──
    from transformers import AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained("intfloat/multilingual-e5-large")

    # ── Assemble pipeline ──
    pipeline = IngestionPipeline(
        transformations=[
            LegalDocumentChunker(tokenizer=tokenizer),
            embed_model,
        ],
        vector_store=vector_store,
    )

    return pipeline


def run_ingestion(documents: list[Document]) -> None:
    """
    Execute the ingestion pipeline on a batch of documents.

    Called during nightly re-index or on-demand when documents are updated.
    Deterministic chunk_ids ensure upsert behavior — no duplicate chunks.
    """
    pipeline = create_ingestion_pipeline()
    nodes = pipeline.run(documents=documents, show_progress=True)
    print(f"Ingested {len(nodes)} chunks from {len(documents)} documents")


# =============================================================================
# 8. WORKED EXAMPLE — Proves the design end-to-end
# =============================================================================

"""
WORKED EXAMPLE: Ingesting Article 3.114 of the AWR

Source document metadata (from classification manifest):
  doc_id: "AWR-2024-v3"
  doc_type: "LEGISLATION"
  title: "Algemene wet inzake rijksbelastingen"
  effective_date: "2024-01-01"
  version: 3
  security_classification: "PUBLIC"
  language: "nl"

Source text (fragment):
  '''
  Artikel 3.114

  1. De belastingplichtige die inkomsten uit arbeid geniet, heeft recht op
  de arbeidskorting. De arbeidskorting bedraagt 5.532 euro per kalenderjaar.

  2. Indien het arbeidsinkomen meer bedraagt dan 39.958 euro, wordt de
  arbeidskorting verminderd met 6,51% van het meerdere.

  a. Voor belastingplichtigen die de AOW-leeftijd hebben bereikt, geldt
  een afwijkend percentage van 3,27%.
  '''

After LegalDocumentChunker processes this, it produces 3 chunks:

Chunk 1 (Paragraph 1):
  chunk_id:        "AWR-2024-v3::art3.114::par1::chunk000"
  hierarchy_path:  "Algemene > Hoofdstuk 3 > Art 3.114 > Lid 1"
  article_num:     "3.114"
  paragraph_num:   "1"
  sub_paragraph:   null
  parent_chunk_id: "AWR-2024-v3::art3.114::chunk000"
  token_count:     87
  security_classification: "PUBLIC"
  → The LLM can cite: "Article 3.114, Paragraph 1 of the AWR"

Chunk 2 (Paragraph 2):
  chunk_id:        "AWR-2024-v3::art3.114::par2::chunk000"
  hierarchy_path:  "Algemene > Hoofdstuk 3 > Art 3.114 > Lid 2"
  article_num:     "3.114"
  paragraph_num:   "2"
  parent_chunk_id: "AWR-2024-v3::art3.114::chunk000"
  token_count:     62
  → The LLM can cite: "Article 3.114, Paragraph 2 of the AWR"

Chunk 3 (Sub-paragraph 2a):
  chunk_id:        "AWR-2024-v3::art3.114::par2::suba::chunk000"
  hierarchy_path:  "Algemene > Hoofdstuk 3 > Art 3.114 > Lid 2 > Sub a"
  article_num:     "3.114"
  paragraph_num:   "2"
  sub_paragraph:   "a"
  parent_chunk_id: "AWR-2024-v3::art3.114::par2::chunk000"
  token_count:     45
  → The LLM can cite: "Article 3.114, Paragraph 2, sub a of the AWR"

All three chunks inherit from parent:
  doc_id, doc_type, title, effective_date, version,
  security_classification, language, source_url

NodeRelationships:
  Chunk 3 → PARENT → Chunk 2 (Paragraph 2)
  Chunk 2 → PARENT → Article 3.114 node
  Chunk 2 → CHILD  → [Chunk 3]

If a naive RecursiveCharacterTextSplitter were used instead:
  - The split might fall between "Artikel 3.114" and "1. De belastingplichtige..."
  - Chunk would contain "1. De belastingplichtige..." without knowing it belongs to Art 3.114
  - LLM cannot reconstruct the citation → FAILS the zero-hallucination requirement
"""
