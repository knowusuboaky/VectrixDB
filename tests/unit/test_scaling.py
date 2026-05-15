"""
Tests for VectrixDB auto-scaling functionality.
"""

import pytest

from vectrixdb import ScalingStrategy, ScalingConfig, AutoScaler


class TestScalingStrategy:
    """Test ScalingStrategy enum."""

    def test_scaling_strategies_exist(self):
        """Test scaling strategy enum values."""
        # Check strategies exist
        assert ScalingStrategy is not None
        assert hasattr(ScalingStrategy, 'NONE')
        assert hasattr(ScalingStrategy, 'BALANCED')


class TestScalingConfig:
    """Test ScalingConfig dataclass."""

    def test_config_creation(self):
        """Test scaling config creation."""
        config = ScalingConfig()
        assert config is not None

    def test_config_with_strategy(self):
        """Test config with strategy."""
        config = ScalingConfig(strategy=ScalingStrategy.NONE)
        assert config.strategy == ScalingStrategy.NONE


class TestAutoScaler:
    """Test AutoScaler class."""

    def test_auto_scaler_exists(self):
        """Test AutoScaler class exists."""
        assert AutoScaler is not None

    def test_auto_scaler_creation(self):
        """Test creating auto scaler."""
        config = ScalingConfig()
        scaler = AutoScaler(config)
        assert scaler is not None


class TestScalingIntegration:
    """Test scaling integration with VectrixDB."""

    def test_vectrixdb_with_scaling_config(self):
        """Test VectrixDB accepts scaling config."""
        from vectrixdb import VectrixDB

        scaling_config = ScalingConfig()
        db = VectrixDB(scaling_config=scaling_config)

        assert db is not None
        db.close()
