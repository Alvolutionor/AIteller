# tests/collectors/conftest.py
"""
Patch asyncio.sleep to be a no-op during collector tests so rate-limit
delays do not slow down the test suite.
"""
import pytest
from unittest.mock import AsyncMock, patch


@pytest.fixture(autouse=True)
def fast_sleep():
    """Replace asyncio.sleep with an instant no-op for all collector tests."""
    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        yield mock_sleep
