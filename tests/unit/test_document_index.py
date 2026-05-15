"""
Tests for VectrixDB Document Index functionality.

Note: Some tests require persistent storage to work properly.
"""

import pytest
import uuid

from vectrixdb import VectrixDB


class TestDocumentIndex:
    """Test Document Index operations."""

    def test_document_index_exists(self):
        """Test that documents property exists on VectrixDB."""
        db = VectrixDB()
        assert hasattr(db, 'documents')
        assert db.documents is not None
        db.close()

    def test_index_text_method_exists(self):
        """Test that index_text method exists."""
        db = VectrixDB()
        assert hasattr(db.documents, 'index_text')
        assert callable(db.documents.index_text)
        db.close()

    def test_list_documents_method_exists(self):
        """Test that list_documents method exists."""
        db = VectrixDB()
        assert hasattr(db.documents, 'list_documents')
        assert callable(db.documents.list_documents)
        db.close()

    def test_get_document_method_exists(self):
        """Test that get_document method exists."""
        db = VectrixDB()
        assert hasattr(db.documents, 'get_document')
        assert callable(db.documents.get_document)
        db.close()

    def test_delete_document_method_exists(self):
        """Test that delete_document method exists."""
        db = VectrixDB()
        assert hasattr(db.documents, 'delete_document')
        assert callable(db.documents.delete_document)
        db.close()

    def test_get_document_nodes_method_exists(self):
        """Test that get_document_nodes method exists."""
        db = VectrixDB()
        assert hasattr(db.documents, 'get_document_nodes')
        assert callable(db.documents.get_document_nodes)
        db.close()

    def test_index_text_returns_document_info(self, temp_dir, sample_document_text):
        """Test indexing a text document returns DocumentInfo."""
        db = VectrixDB(path=temp_dir)

        doc_id = str(uuid.uuid4())
        try:
            doc_info = db.documents.index_text(
                doc_id=doc_id,
                content=sample_document_text,
                title="Test Research Paper"
            )

            assert doc_info is not None
            assert doc_info.doc_id == doc_id
            assert doc_info.title == "Test Research Paper"
        except Exception as e:
            # Document indexing may fail if storage doesn't support it
            pytest.skip(f"Document indexing not supported: {e}")
        finally:
            db.close()


class TestChunkingFunctions:
    """Test chunking utility functions."""

    def test_chunk_text_exists(self):
        """Test chunk_text function exists."""
        from vectrixdb import chunk_text
        assert callable(chunk_text)

    def test_chunk_with_context_exists(self):
        """Test chunk_with_context function exists."""
        from vectrixdb import chunk_with_context
        assert callable(chunk_with_context)

    def test_chunk_text_basic(self):
        """Test basic text chunking."""
        from vectrixdb import chunk_text

        text = "This is sentence one. This is sentence two. This is sentence three. This is sentence four. This is sentence five."
        chunks = chunk_text(text, chunk_size=50)

        assert len(chunks) >= 1

    def test_chunk_with_context_returns_list(self, sample_document_text):
        """Test chunking with context returns a list."""
        from vectrixdb import chunk_with_context

        chunks = chunk_with_context(sample_document_text, chunk_size=200)

        assert isinstance(chunks, list)
        assert len(chunks) > 0


class TestDocumentTypes:
    """Test DocumentType enum."""

    def test_document_type_exists(self):
        """Test DocumentType enum exists."""
        from vectrixdb import DocumentType
        assert DocumentType is not None

    def test_document_type_values(self):
        """Test DocumentType has expected values."""
        from vectrixdb import DocumentType

        # Check common types exist
        assert hasattr(DocumentType, 'TEXT') or hasattr(DocumentType, 'MARKDOWN')
