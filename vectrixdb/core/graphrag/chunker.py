"""
Document Chunker for GraphRAG.

Splits documents into overlapping chunks (TextUnits) while preserving
sentence boundaries and tracking source document metadata.
"""

import re
import uuid
import hashlib
from dataclasses import dataclass, field
from typing import List, Optional, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import GraphRAGConfig


@dataclass
class TextUnit:
    """
    A chunk of text extracted from a document.

    Represents the atomic unit for entity extraction in GraphRAG.
    Each TextUnit maintains a reference back to its source document.
    """
    id: str
    """Unique identifier for this text unit."""

    text: str
    """The actual text content."""

    doc_id: str
    """ID of the source document."""

    position: int
    """Position/index of this chunk within the document (0-indexed)."""

    token_count: int
    """Approximate number of tokens in this chunk."""

    char_start: int = 0
    """Character offset where this chunk starts in the original document."""

    char_end: int = 0
    """Character offset where this chunk ends in the original document."""

    metadata: dict = field(default_factory=dict)
    """Additional metadata inherited from the document."""

    def __hash__(self):
        return hash(self.id)

    def __eq__(self, other):
        if isinstance(other, TextUnit):
            return self.id == other.id
        return False


class DocumentChunker:
    """
    Split documents into overlapping chunks with metadata preservation.

    Uses sentence-aware chunking to avoid breaking mid-sentence,
    with configurable chunk size and overlap.

    Example:
        >>> chunker = DocumentChunker(chunk_size=1200, chunk_overlap=100)
        >>> text_units = chunker.chunk("Long document text...", doc_id="doc1")
        >>> for unit in text_units:
        ...     print(f"Chunk {unit.position}: {len(unit.text)} chars")
    """

    # Simple sentence boundary pattern
    SENTENCE_PATTERN = re.compile(r'(?<=[.!?])\s+(?=[A-Z])')

    # Approximate tokens per character (for English text)
    CHARS_PER_TOKEN = 4

    def __init__(
        self,
        chunk_size: int = 1200,
        chunk_overlap: int = 100,
        chunk_by_sentence: bool = True,
        config: Optional[GraphRAGConfig] = None
    ):
        """
        Initialize the document chunker.

        Args:
            chunk_size: Target number of tokens per chunk.
            chunk_overlap: Number of overlapping tokens between chunks.
            chunk_by_sentence: Whether to preserve sentence boundaries.
            config: Optional GraphRAGConfig to use settings from.
        """
        if config:
            self.chunk_size = config.chunk_size
            self.chunk_overlap = config.chunk_overlap
            self.chunk_by_sentence = config.chunk_by_sentence
        else:
            self.chunk_size = chunk_size
            self.chunk_overlap = chunk_overlap
            self.chunk_by_sentence = chunk_by_sentence

        # Convert token counts to approximate character counts
        self.target_chars = self.chunk_size * self.CHARS_PER_TOKEN
        self.overlap_chars = self.chunk_overlap * self.CHARS_PER_TOKEN

    def _generate_unit_id(self, doc_id: str, position: int, text: str) -> str:
        """Generate a deterministic ID for a text unit."""
        content_hash = hashlib.md5(text.encode()).hexdigest()[:8]
        return f"{doc_id}_chunk_{position}_{content_hash}"

    def _estimate_tokens(self, text: str) -> int:
        """Estimate the number of tokens in text."""
        return len(text) // self.CHARS_PER_TOKEN

    def _split_into_sentences(self, text: str) -> List[str]:
        """Split text into sentences."""
        # Use regex to split on sentence boundaries
        sentences = self.SENTENCE_PATTERN.split(text)
        # Filter out empty sentences and strip whitespace
        return [s.strip() for s in sentences if s.strip()]

    def _split_by_paragraphs(self, text: str) -> List[str]:
        """Split text by paragraph boundaries."""
        # Split on double newlines or multiple newlines
        paragraphs = re.split(r'\n\s*\n', text)
        return [p.strip() for p in paragraphs if p.strip()]

    def chunk(
        self,
        text: str,
        doc_id: str,
        metadata: Optional[dict] = None
    ) -> List[TextUnit]:
        """
        Split a document into overlapping text units.

        Args:
            text: The document text to chunk.
            doc_id: Unique identifier for the source document.
            metadata: Optional metadata to attach to each chunk.

        Returns:
            List of TextUnit objects.
        """
        if not text or not text.strip():
            return []

        metadata = metadata or {}
        text = text.strip()

        # Handle very short documents
        if len(text) <= self.target_chars:
            return [TextUnit(
                id=self._generate_unit_id(doc_id, 0, text),
                text=text,
                doc_id=doc_id,
                position=0,
                token_count=self._estimate_tokens(text),
                char_start=0,
                char_end=len(text),
                metadata=metadata
            )]

        if self.chunk_by_sentence:
            return self._chunk_by_sentences(text, doc_id, metadata)
        else:
            return self._chunk_by_characters(text, doc_id, metadata)

    def _chunk_by_sentences(
        self,
        text: str,
        doc_id: str,
        metadata: dict
    ) -> List[TextUnit]:
        """Chunk text while preserving sentence boundaries."""
        sentences = self._split_into_sentences(text)

        if not sentences:
            # Fallback to character-based chunking
            return self._chunk_by_characters(text, doc_id, metadata)

        chunks = []
        current_chunk = []
        current_length = 0
        char_offset = 0

        for i, sentence in enumerate(sentences):
            sentence_length = len(sentence)

            # If adding this sentence exceeds target, save current chunk
            if current_length + sentence_length > self.target_chars and current_chunk:
                # Save the current chunk
                chunk_text = ' '.join(current_chunk)
                chunk_start = char_offset - current_length

                chunks.append(TextUnit(
                    id=self._generate_unit_id(doc_id, len(chunks), chunk_text),
                    text=chunk_text,
                    doc_id=doc_id,
                    position=len(chunks),
                    token_count=self._estimate_tokens(chunk_text),
                    char_start=max(0, chunk_start),
                    char_end=char_offset,
                    metadata=metadata
                ))

                # Calculate overlap: keep last N characters worth of sentences
                overlap_sentences = []
                overlap_length = 0
                for s in reversed(current_chunk):
                    if overlap_length + len(s) <= self.overlap_chars:
                        overlap_sentences.insert(0, s)
                        overlap_length += len(s) + 1  # +1 for space
                    else:
                        break

                current_chunk = overlap_sentences
                current_length = overlap_length

            current_chunk.append(sentence)
            current_length += sentence_length + 1  # +1 for space
            char_offset += sentence_length + 1

        # Don't forget the last chunk
        if current_chunk:
            chunk_text = ' '.join(current_chunk)
            chunk_start = char_offset - current_length

            chunks.append(TextUnit(
                id=self._generate_unit_id(doc_id, len(chunks), chunk_text),
                text=chunk_text,
                doc_id=doc_id,
                position=len(chunks),
                token_count=self._estimate_tokens(chunk_text),
                char_start=max(0, chunk_start),
                char_end=len(text),
                metadata=metadata
            ))

        return chunks

    def _chunk_by_characters(
        self,
        text: str,
        doc_id: str,
        metadata: dict
    ) -> List[TextUnit]:
        """Simple character-based chunking with overlap."""
        chunks = []
        start = 0

        while start < len(text):
            end = min(start + self.target_chars, len(text))

            # Try to end at a word boundary
            if end < len(text):
                # Look for last space within the target range
                last_space = text.rfind(' ', start, end)
                if last_space > start:
                    end = last_space

            chunk_text = text[start:end].strip()

            if chunk_text:
                chunks.append(TextUnit(
                    id=self._generate_unit_id(doc_id, len(chunks), chunk_text),
                    text=chunk_text,
                    doc_id=doc_id,
                    position=len(chunks),
                    token_count=self._estimate_tokens(chunk_text),
                    char_start=start,
                    char_end=end,
                    metadata=metadata
                ))

            # Move start forward, accounting for overlap
            start = end - self.overlap_chars
            if start >= end:
                start = end

        return chunks

    def chunk_batch(
        self,
        texts: List[str],
        doc_ids: List[str],
        metadata_list: Optional[List[dict]] = None,
        max_workers: int = 4
    ) -> List[TextUnit]:
        """
        Chunk multiple documents in parallel.

        Args:
            texts: List of document texts.
            doc_ids: List of document IDs (must match texts length).
            metadata_list: Optional list of metadata dicts for each document.
            max_workers: Maximum parallel workers.

        Returns:
            Flat list of all TextUnits from all documents.
        """
        if len(texts) != len(doc_ids):
            raise ValueError("texts and doc_ids must have the same length")

        metadata_list = metadata_list or [{}] * len(texts)
        if len(metadata_list) != len(texts):
            raise ValueError("metadata_list must have the same length as texts")

        all_units = []

        # For small batches, just process sequentially
        if len(texts) <= 10:
            for text, doc_id, metadata in zip(texts, doc_ids, metadata_list):
                units = self.chunk(text, doc_id, metadata)
                all_units.extend(units)
            return all_units

        # Parallel processing for larger batches
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self.chunk, text, doc_id, metadata): i
                for i, (text, doc_id, metadata) in enumerate(zip(texts, doc_ids, metadata_list))
            }

            # Collect results in order
            results = [None] * len(texts)
            for future in as_completed(futures):
                idx = futures[future]
                try:
                    results[idx] = future.result()
                except Exception as e:
                    # On error, create a single chunk with the whole document
                    results[idx] = [TextUnit(
                        id=f"{doc_ids[idx]}_error",
                        text=texts[idx][:self.target_chars],
                        doc_id=doc_ids[idx],
                        position=0,
                        token_count=self._estimate_tokens(texts[idx]),
                        char_start=0,
                        char_end=len(texts[idx]),
                        metadata={"error": str(e)}
                    )]

            # Flatten results
            for units in results:
                if units:
                    all_units.extend(units)

        return all_units

    def chunk_iterator(
        self,
        texts: Iterator[str],
        doc_ids: Iterator[str],
        metadata_iterator: Optional[Iterator[dict]] = None
    ) -> Iterator[TextUnit]:
        """
        Streaming chunker for large document collections.

        Yields TextUnits one at a time to minimize memory usage.

        Args:
            texts: Iterator of document texts.
            doc_ids: Iterator of document IDs.
            metadata_iterator: Optional iterator of metadata dicts.

        Yields:
            TextUnit objects.
        """
        if metadata_iterator is None:
            metadata_iterator = iter(lambda: {}, None)

        for text, doc_id, metadata in zip(texts, doc_ids, metadata_iterator):
            for unit in self.chunk(text, doc_id, metadata or {}):
                yield unit


def create_chunker(config: Optional[GraphRAGConfig] = None) -> DocumentChunker:
    """Factory function to create a DocumentChunker."""
    if config:
        return DocumentChunker(config=config)
    return DocumentChunker()
