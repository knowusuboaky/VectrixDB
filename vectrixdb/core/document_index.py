"""
VectrixDB Document Index - Hierarchical document indexing with tree structure.

Provides page/section navigation alongside vector search:
- Tree structure from documents (PDF pages, markdown headings)
- Smart chunking with overlap and context preservation
- Document lifecycle tracking (indexed_at, last_synced, etag)
- Integration with VectrixDB storage backends

Author: Daddy Nyame Owusu - Boakye
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from .storage import BaseStorage


class DocumentType(str, Enum):
    """Supported document types."""
    PDF = "pdf"
    MARKDOWN = "markdown"
    TEXT = "text"
    HTML = "html"


@dataclass
class DocumentNode:
    """A node in the document tree structure."""
    node_id: str
    doc_id: str
    parent_id: Optional[str] = None
    level: int = 1
    title: str = ""
    text: str = ""
    summary: str = ""
    page_num: Optional[int] = None
    position: int = 0  # Order within siblings
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "doc_id": self.doc_id,
            "parent_id": self.parent_id,
            "level": self.level,
            "title": self.title,
            "text": self.text,
            "summary": self.summary,
            "page_num": self.page_num,
            "position": self.position,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DocumentNode":
        return cls(
            node_id=data["node_id"],
            doc_id=data["doc_id"],
            parent_id=data.get("parent_id"),
            level=data.get("level", 1),
            title=data.get("title", ""),
            text=data.get("text", ""),
            summary=data.get("summary", ""),
            page_num=data.get("page_num"),
            position=data.get("position", 0),
            metadata=data.get("metadata", {}),
        )


@dataclass
class DocumentInfo:
    """Document metadata and index information."""
    doc_id: str
    title: str
    doc_type: DocumentType
    source_path: Optional[str] = None
    etag: Optional[str] = None
    content_hash: Optional[str] = None
    page_count: int = 0
    section_count: int = 0
    node_count: int = 0
    indexed_at: Optional[datetime] = None
    last_synced: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "title": self.title,
            "doc_type": self.doc_type.value,
            "source_path": self.source_path,
            "etag": self.etag,
            "content_hash": self.content_hash,
            "page_count": self.page_count,
            "section_count": self.section_count,
            "node_count": self.node_count,
            "indexed_at": self.indexed_at.isoformat() if self.indexed_at else None,
            "last_synced": self.last_synced.isoformat() if self.last_synced else None,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DocumentInfo":
        indexed_at = None
        if data.get("indexed_at"):
            indexed_at = datetime.fromisoformat(data["indexed_at"].replace("Z", "+00:00"))

        last_synced = None
        if data.get("last_synced"):
            last_synced = datetime.fromisoformat(data["last_synced"].replace("Z", "+00:00"))

        return cls(
            doc_id=data["doc_id"],
            title=data.get("title", ""),
            doc_type=DocumentType(data.get("doc_type", "text")),
            source_path=data.get("source_path"),
            etag=data.get("etag"),
            content_hash=data.get("content_hash"),
            page_count=data.get("page_count", 0),
            section_count=data.get("section_count", 0),
            node_count=data.get("node_count", 0),
            indexed_at=indexed_at,
            last_synced=last_synced,
            metadata=data.get("metadata", {}),
        )


@dataclass
class ChunkInfo:
    """Information about a text chunk for vectorization."""
    chunk_id: str
    doc_id: str
    node_id: str
    text: str
    heading: Optional[str] = None
    level: int = 0
    page_num: Optional[int] = None
    position: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SearchResultWithContext:
    """Search result with document context."""
    chunk_id: str
    doc_id: str
    node_id: str
    text: str
    score: float
    page_num: Optional[int] = None
    section_path: List[str] = field(default_factory=list)  # ["Chapter 1", "Introduction", "Overview"]
    document_title: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


# =============================================================================
# Chunking Utilities
# =============================================================================

def chunk_text(
    text: str,
    chunk_size: int = 1000,
    chunk_overlap: int = 200,
    separators: Optional[List[str]] = None,
) -> List[str]:
    """Split text into overlapping chunks.

    Args:
        text: The text to chunk.
        chunk_size: Maximum size of each chunk.
        chunk_overlap: Number of characters to overlap between chunks.
        separators: List of separators to split on (in order of preference).

    Returns:
        List of text chunks.
    """
    if not text:
        return []

    if separators is None:
        separators = ["\n\n", "\n", ". ", " ", ""]

    def split_on_separator(text: str, separator: str) -> List[str]:
        if separator:
            return text.split(separator)
        return list(text)

    def merge_chunks(splits: List[str], separator: str) -> List[str]:
        chunks = []
        current_chunk = []
        current_size = 0

        for split in splits:
            split_size = len(split) + len(separator)

            if current_size + split_size > chunk_size and current_chunk:
                chunk_text = separator.join(current_chunk)
                chunks.append(chunk_text)

                overlap_size = 0
                overlap_chunks = []
                for prev in reversed(current_chunk):
                    if overlap_size + len(prev) > chunk_overlap:
                        break
                    overlap_chunks.insert(0, prev)
                    overlap_size += len(prev) + len(separator)

                current_chunk = overlap_chunks
                current_size = overlap_size

            current_chunk.append(split)
            current_size += split_size

        if current_chunk:
            chunks.append(separator.join(current_chunk))

        return chunks

    for separator in separators:
        splits = split_on_separator(text, separator)
        if all(len(s) <= chunk_size for s in splits):
            return merge_chunks(splits, separator)

    chunks = []
    for i in range(0, len(text), chunk_size - chunk_overlap):
        chunks.append(text[i:i + chunk_size])
    return chunks


def chunk_with_context(
    text: str,
    chunk_size: int = 1000,
    chunk_overlap: int = 200,
) -> List[Dict[str, Any]]:
    """Split markdown into chunks, preserving heading context.

    Args:
        text: Markdown text to chunk.
        chunk_size: Maximum size of each chunk.
        chunk_overlap: Number of characters to overlap.

    Returns:
        List of dicts with 'content', 'heading', and 'level' keys.
    """
    if not text:
        return []

    heading_pattern = re.compile(r'^(#{1,6})\s+(.+)$', re.MULTILINE)

    chunks = []
    current_heading = None
    current_level = 0
    last_pos = 0

    for match in heading_pattern.finditer(text):
        content = text[last_pos:match.start()].strip()
        if content:
            for chunk in chunk_text(content, chunk_size, chunk_overlap):
                chunks.append({
                    "content": chunk,
                    "heading": current_heading,
                    "level": current_level,
                })

        current_level = len(match.group(1))
        current_heading = match.group(2)
        last_pos = match.end()

    content = text[last_pos:].strip()
    if content:
        for chunk in chunk_text(content, chunk_size, chunk_overlap):
            chunks.append({
                "content": chunk,
                "heading": current_heading,
                "level": current_level,
            })

    return chunks


# =============================================================================
# Tree Building Utilities
# =============================================================================

def _generate_summary(text: str, max_length: int = 200) -> str:
    """Generate a simple summary (first sentence or truncated)."""
    if not text:
        return ""

    sentence_end = re.search(r'[.!?]\s', text)
    if sentence_end and sentence_end.end() <= max_length:
        return text[:sentence_end.end()].strip()

    if len(text) <= max_length:
        return text

    return text[:max_length - 3].strip() + "..."


def build_tree_from_markdown(
    text: str,
    doc_id: str,
    generate_summaries: bool = True,
    max_summary_length: int = 200,
) -> List[DocumentNode]:
    """Build tree structure from markdown headings.

    Args:
        text: Markdown text content.
        doc_id: Document ID for linking nodes.
        generate_summaries: Whether to generate section summaries.
        max_summary_length: Maximum length of summaries.

    Returns:
        List of DocumentNode objects (flat, with parent_id for hierarchy).
    """
    heading_pattern = re.compile(r'^(#{1,6})\s+(.+)$', re.MULTILINE)

    sections = []
    node_counter = [0]
    last_pos = 0

    def get_node_id() -> str:
        node_counter[0] += 1
        return f"{doc_id}:node:{node_counter[0]:04d}"

    matches = list(heading_pattern.finditer(text))

    for i, match in enumerate(matches):
        content_before = text[last_pos:match.start()].strip()

        level = len(match.group(1))
        title = match.group(2).strip()

        # Get content after this heading (until next heading or end)
        if i + 1 < len(matches):
            content = text[match.end():matches[i + 1].start()].strip()
        else:
            content = text[match.end():].strip()

        node = DocumentNode(
            node_id=get_node_id(),
            doc_id=doc_id,
            level=level,
            title=title,
            text=content,
            summary=_generate_summary(content, max_summary_length) if generate_summaries else "",
            position=i,
        )
        sections.append(node)
        last_pos = match.end()

    # Build parent relationships based on levels
    _assign_parents(sections)

    return sections


def build_tree_from_pdf(
    text: str,
    doc_id: str,
    generate_summaries: bool = True,
    max_summary_length: int = 200,
) -> List[DocumentNode]:
    """Build tree structure from PDF pages.

    Expects text with page markers like: --- Page 1 ---

    Args:
        text: PDF text with page markers.
        doc_id: Document ID for linking nodes.
        generate_summaries: Whether to generate page summaries.
        max_summary_length: Maximum length of summaries.

    Returns:
        List of DocumentNode objects (one per page).
    """
    page_pattern = re.compile(r'---\s*Page\s*(\d+)\s*---', re.IGNORECASE)
    pages = page_pattern.split(text)

    nodes = []
    for i in range(1, len(pages), 2):
        page_num = int(pages[i])
        content = pages[i + 1].strip() if i + 1 < len(pages) else ""

        node = DocumentNode(
            node_id=f"{doc_id}:page:{page_num:04d}",
            doc_id=doc_id,
            level=1,
            title=f"Page {page_num}",
            text=content,
            summary=_generate_summary(content, max_summary_length) if generate_summaries else "",
            page_num=page_num,
            position=page_num - 1,
        )
        nodes.append(node)

    return nodes


def build_tree_from_text(
    text: str,
    doc_id: str,
    generate_summaries: bool = True,
    max_summary_length: int = 200,
) -> List[DocumentNode]:
    """Build simple tree from plain text (paragraphs).

    Args:
        text: Plain text content.
        doc_id: Document ID for linking nodes.
        generate_summaries: Whether to generate summaries.
        max_summary_length: Maximum length of summaries.

    Returns:
        List of DocumentNode objects (one per paragraph).
    """
    paragraphs = text.split("\n\n")

    nodes = []
    for i, para in enumerate(paragraphs, 1):
        para = para.strip()
        if not para:
            continue

        node = DocumentNode(
            node_id=f"{doc_id}:para:{i:04d}",
            doc_id=doc_id,
            level=1,
            title=f"Section {i}",
            text=para,
            summary=_generate_summary(para, max_summary_length) if generate_summaries else "",
            position=i - 1,
        )
        nodes.append(node)

    return nodes


def _assign_parents(nodes: List[DocumentNode]) -> None:
    """Assign parent_id based on heading levels (modifies nodes in place)."""
    if not nodes:
        return

    stack: List[DocumentNode] = []

    for node in nodes:
        # Pop stack until we find a parent with lower level
        while stack and stack[-1].level >= node.level:
            stack.pop()

        # Assign parent
        if stack:
            node.parent_id = stack[-1].node_id

        stack.append(node)


def get_section_path(
    node: DocumentNode,
    nodes_by_id: Dict[str, DocumentNode],
) -> List[str]:
    """Get the full path from root to this node.

    Args:
        node: The target node.
        nodes_by_id: Dict mapping node_id to DocumentNode.

    Returns:
        List of titles from root to node, e.g. ["Chapter 1", "Introduction", "Overview"]
    """
    path = [node.title]
    current = node

    while current.parent_id and current.parent_id in nodes_by_id:
        current = nodes_by_id[current.parent_id]
        path.insert(0, current.title)

    return path


# =============================================================================
# Document Index Class
# =============================================================================

class DocumentIndex:
    """
    Document indexer with tree structure and chunking support.

    Integrates with VectrixDB storage backends to provide:
    - Hierarchical document navigation (pages, sections)
    - Smart chunking for vectorization
    - Document lifecycle tracking
    - Search with context

    Example:
        >>> from vectrixdb import VectrixDB
        >>> db = VectrixDB("./data")
        >>>
        >>> # Index a markdown document
        >>> doc_info = db.documents.index_text(
        ...     doc_id="readme",
        ...     content="# Introduction\\n...",
        ...     doc_type="markdown"
        ... )
        >>>
        >>> # Get chunks for vectorization
        >>> chunks = db.documents.get_chunks("readme")
        >>>
        >>> # Navigate to a section
        >>> section = db.documents.get_node("readme:node:0001")
    """

    def __init__(self, storage: "BaseStorage"):
        """Initialize DocumentIndex with a storage backend.

        Args:
            storage: VectrixDB storage backend instance.
        """
        self._storage = storage
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        """Ensure document and node tables exist in storage."""
        # This will be called by the storage backend
        self._storage.ensure_document_tables()

    # =========================================================================
    # Indexing Methods
    # =========================================================================

    def index_text(
        self,
        doc_id: str,
        content: str,
        title: Optional[str] = None,
        doc_type: str = "markdown",
        metadata: Optional[Dict[str, Any]] = None,
        chunk_size: int = 1000,
        chunk_overlap: int = 200,
    ) -> DocumentInfo:
        """Index text content directly.

        Args:
            doc_id: Unique document ID.
            content: Document text content.
            title: Document title (defaults to doc_id).
            doc_type: Type ("markdown", "pdf", "text").
            metadata: Additional metadata.
            chunk_size: Size of chunks for vectorization.
            chunk_overlap: Overlap between chunks.

        Returns:
            DocumentInfo with indexing results.
        """
        doc_type_enum = DocumentType(doc_type)
        content_hash = hashlib.md5(content.encode()).hexdigest()

        # Check if already indexed with same content
        existing = self.get_document(doc_id)
        if existing and existing.content_hash == content_hash:
            existing.last_synced = datetime.now(timezone.utc)
            self._storage.save_document(existing.to_dict())
            return existing

        # Build tree structure based on document type
        if doc_type_enum == DocumentType.MARKDOWN:
            nodes = build_tree_from_markdown(content, doc_id)
        elif doc_type_enum == DocumentType.PDF:
            nodes = build_tree_from_pdf(content, doc_id)
        else:
            nodes = build_tree_from_text(content, doc_id)

        # Count pages vs sections
        page_count = sum(1 for n in nodes if n.page_num is not None)
        section_count = len(nodes) - page_count

        # Create document info
        now = datetime.now(timezone.utc)
        doc_info = DocumentInfo(
            doc_id=doc_id,
            title=title or doc_id,
            doc_type=doc_type_enum,
            content_hash=content_hash,
            page_count=page_count,
            section_count=section_count,
            node_count=len(nodes),
            indexed_at=now,
            last_synced=now,
            metadata=metadata or {},
        )

        # Save document and nodes
        self._storage.save_document(doc_info.to_dict())

        # Delete old nodes first
        self._storage.delete_document_nodes(doc_id)

        # Save new nodes
        for node in nodes:
            self._storage.save_node(node.to_dict())

        return doc_info

    def index_file(
        self,
        file_path: str,
        doc_id: Optional[str] = None,
        doc_type: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        chunk_size: int = 1000,
        chunk_overlap: int = 200,
    ) -> DocumentInfo:
        """Index a file from local filesystem.

        Args:
            file_path: Path to the file.
            doc_id: Document ID (defaults to filename).
            doc_type: Document type (auto-detected from extension if not provided).
            metadata: Additional metadata.
            chunk_size: Size of chunks for vectorization.
            chunk_overlap: Overlap between chunks.

        Returns:
            DocumentInfo with indexing results.
        """
        path = Path(file_path)

        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        # Auto-detect document type
        if doc_type is None:
            ext = path.suffix.lower()
            if ext == ".pdf":
                doc_type = "pdf"
            elif ext in (".md", ".markdown"):
                doc_type = "markdown"
            else:
                doc_type = "text"

        # Read content
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()

        # Use filename as doc_id if not provided
        if doc_id is None:
            doc_id = path.stem

        # Add file metadata
        file_metadata = {
            "source_path": str(path.absolute()),
            "file_size": path.stat().st_size,
            "file_modified": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(),
        }
        if metadata:
            file_metadata.update(metadata)

        doc_info = self.index_text(
            doc_id=doc_id,
            content=content,
            title=path.stem.replace("-", " ").replace("_", " ").title(),
            doc_type=doc_type,
            metadata=file_metadata,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )

        doc_info.source_path = str(path.absolute())
        self._storage.save_document(doc_info.to_dict())

        return doc_info

    # =========================================================================
    # Query Methods
    # =========================================================================

    def get_document(self, doc_id: str) -> Optional[DocumentInfo]:
        """Get document info by ID.

        Args:
            doc_id: Document ID.

        Returns:
            DocumentInfo or None if not found.
        """
        data = self._storage.get_document(doc_id)
        if data:
            return DocumentInfo.from_dict(data)
        return None

    def get_node(self, node_id: str) -> Optional[DocumentNode]:
        """Get a specific node by ID.

        Args:
            node_id: Node ID.

        Returns:
            DocumentNode or None if not found.
        """
        data = self._storage.get_node(node_id)
        if data:
            return DocumentNode.from_dict(data)
        return None

    def get_page(self, doc_id: str, page_num: int) -> Optional[DocumentNode]:
        """Get a specific page from a PDF document.

        Args:
            doc_id: Document ID.
            page_num: Page number (1-indexed).

        Returns:
            DocumentNode for the page or None if not found.
        """
        nodes = self.get_document_nodes(doc_id)
        for node in nodes:
            if node.page_num == page_num:
                return node
        return None

    def get_section(self, doc_id: str, section_title: str) -> Optional[DocumentNode]:
        """Get a section by title.

        Args:
            doc_id: Document ID.
            section_title: Section title to find.

        Returns:
            DocumentNode for the section or None if not found.
        """
        nodes = self.get_document_nodes(doc_id)
        for node in nodes:
            if node.title.lower() == section_title.lower():
                return node
        return None

    def get_document_nodes(self, doc_id: str) -> List[DocumentNode]:
        """Get all nodes for a document.

        Args:
            doc_id: Document ID.

        Returns:
            List of DocumentNode objects.
        """
        nodes_data = self._storage.get_document_nodes(doc_id)
        return [DocumentNode.from_dict(d) for d in nodes_data]

    def get_children(self, node_id: str) -> List[DocumentNode]:
        """Get child nodes of a given node.

        Args:
            node_id: Parent node ID.

        Returns:
            List of child DocumentNode objects.
        """
        nodes_data = self._storage.get_child_nodes(node_id)
        return [DocumentNode.from_dict(d) for d in nodes_data]

    def get_section_path(self, node_id: str) -> List[str]:
        """Get the full path from root to a node.

        Args:
            node_id: Node ID.

        Returns:
            List of titles, e.g. ["Chapter 1", "Introduction", "Overview"]
        """
        node = self.get_node(node_id)
        if not node:
            return []

        nodes = self.get_document_nodes(node.doc_id)
        nodes_by_id = {n.node_id: n for n in nodes}

        return get_section_path(node, nodes_by_id)

    # =========================================================================
    # Chunking Methods
    # =========================================================================

    def get_chunks(
        self,
        doc_id: str,
        chunk_size: int = 1000,
        chunk_overlap: int = 200,
    ) -> List[ChunkInfo]:
        """Get chunks for vectorization from a document.

        Args:
            doc_id: Document ID.
            chunk_size: Maximum chunk size.
            chunk_overlap: Overlap between chunks.

        Returns:
            List of ChunkInfo objects ready for embedding.
        """
        nodes = self.get_document_nodes(doc_id)
        nodes_by_id = {n.node_id: n for n in nodes}
        chunks = []
        chunk_counter = 0

        for node in nodes:
            if not node.text:
                continue

            # Chunk the node text
            text_chunks = chunk_text(node.text, chunk_size, chunk_overlap)
            section_path = get_section_path(node, nodes_by_id)

            for text in text_chunks:
                chunk_counter += 1
                chunk = ChunkInfo(
                    chunk_id=f"{doc_id}:chunk:{chunk_counter:04d}",
                    doc_id=doc_id,
                    node_id=node.node_id,
                    text=text,
                    heading=node.title,
                    level=node.level,
                    page_num=node.page_num,
                    position=chunk_counter,
                    metadata={
                        "section_path": section_path,
                        "summary": node.summary,
                    },
                )
                chunks.append(chunk)

        return chunks

    # =========================================================================
    # Management Methods
    # =========================================================================

    def list_documents(self) -> List[DocumentInfo]:
        """List all indexed documents.

        Returns:
            List of DocumentInfo objects.
        """
        docs_data = self._storage.list_documents()
        return [DocumentInfo.from_dict(d) for d in docs_data]

    def delete_document(self, doc_id: str) -> bool:
        """Delete a document and all its nodes.

        Args:
            doc_id: Document ID to delete.

        Returns:
            True if deleted, False if not found.
        """
        # Delete nodes first
        self._storage.delete_document_nodes(doc_id)
        # Then delete document
        return self._storage.delete_document(doc_id)

    def document_exists(self, doc_id: str) -> bool:
        """Check if a document exists.

        Args:
            doc_id: Document ID.

        Returns:
            True if exists.
        """
        return self.get_document(doc_id) is not None

    def get_stats(self) -> Dict[str, Any]:
        """Get indexing statistics.

        Returns:
            Dict with document and node counts.
        """
        docs = self.list_documents()
        total_nodes = sum(d.node_count for d in docs)
        total_pages = sum(d.page_count for d in docs)
        total_sections = sum(d.section_count for d in docs)

        return {
            "total_documents": len(docs),
            "total_nodes": total_nodes,
            "total_pages": total_pages,
            "total_sections": total_sections,
            "by_type": {
                doc_type.value: sum(1 for d in docs if d.doc_type == doc_type)
                for doc_type in DocumentType
            },
        }
