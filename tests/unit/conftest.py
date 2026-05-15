"""
Pytest configuration and shared fixtures for VectrixDB tests.
"""

import pytest
import tempfile
import shutil
from pathlib import Path


@pytest.fixture
def temp_dir():
    """Create a temporary directory for test databases."""
    tmp = tempfile.mkdtemp(prefix="vectrixdb_test_")
    yield tmp
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def sample_vectors():
    """Sample vectors for testing."""
    return [
        [0.1, 0.2, 0.3, 0.4],
        [0.5, 0.6, 0.7, 0.8],
        [0.9, 0.1, 0.2, 0.3],
        [0.4, 0.5, 0.6, 0.7],
    ]


@pytest.fixture
def sample_texts():
    """Sample texts for testing."""
    return [
        "Python is a great programming language",
        "Machine learning is transforming industries",
        "Vector databases enable semantic search",
        "AI is the future of technology",
    ]


@pytest.fixture
def sample_metadata():
    """Sample metadata for testing."""
    return [
        {"category": "programming", "language": "python"},
        {"category": "ml", "topic": "general"},
        {"category": "database", "type": "vector"},
        {"category": "ai", "topic": "future"},
    ]


@pytest.fixture
def sample_document_text():
    """Sample markdown document for testing."""
    return """# Introduction
This is the introduction section with important content.

## Background
The background provides context for the research.

### Historical Context
Some historical details here.

## Methods
The methods section describes the approach.

### Data Collection
How data was collected.

### Analysis
How data was analyzed.

## Results
The results of the study.

## Conclusion
Final thoughts and conclusions.
"""
