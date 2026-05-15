"""
Tests for VectrixDB quantization functionality.
"""

import pytest

from vectrixdb import QuantizationType, QuantizationConfig


class TestQuantizationType:
    """Test QuantizationType enum."""

    def test_none_exists(self):
        """Test NONE quantization type exists."""
        assert hasattr(QuantizationType, 'NONE')

    def test_scalar_exists(self):
        """Test SCALAR quantization type exists."""
        assert hasattr(QuantizationType, 'SCALAR')

    def test_binary_exists(self):
        """Test BINARY quantization type exists."""
        assert hasattr(QuantizationType, 'BINARY')


class TestQuantizationConfig:
    """Test QuantizationConfig dataclass."""

    def test_config_class_exists(self):
        """Test QuantizationConfig class exists."""
        assert QuantizationConfig is not None

    def test_config_creation(self):
        """Test creating QuantizationConfig."""
        config = QuantizationConfig()
        assert config is not None

    def test_config_with_type(self):
        """Test config with specific type."""
        config = QuantizationConfig(type=QuantizationType.SCALAR)
        assert config.type == QuantizationType.SCALAR


class TestQuantizationIntegration:
    """Test quantization with VectrixDB."""

    def test_collection_creation_works(self):
        """Test creating collection (quantization may be internal)."""
        from vectrixdb import VectrixDB

        db = VectrixDB()
        coll = db.create_collection("quant_test", dimension=4)
        assert coll is not None

        db.close()
