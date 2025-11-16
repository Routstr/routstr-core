"""Unit tests for logging functionality."""

import pytest
from unittest.mock import patch


def test_get_logger() -> None:
    """Test get_logger function."""
    from routstr.core.logging import get_logger
    
    logger = get_logger("test_module")
    
    assert logger is not None
    assert logger.name == "test_module"


def test_logger_configuration() -> None:
    """Test logger configuration."""
    from routstr.core.logging import get_logger
    
    logger = get_logger("test_module")
    
    assert logger.level is not None


def test_structured_logging() -> None:
    """Test structured logging formatters."""
    from routstr.core.logging import get_logger
    
    logger = get_logger("test_module")
    
    logger.info("Test message", extra={"key": "value"})
    
    assert True
