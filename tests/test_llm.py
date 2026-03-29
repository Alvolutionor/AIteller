# tests/test_llm.py
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from src.utils.llm import LLMClient

@pytest.fixture
def llm_config():
    return {
        "llm": {
            "primary": "claude",
            "fallback": "openai",
            "claude": {
                "api_key": "test-claude-key",
                "fast_model": "claude-haiku-4-5-20251001",
                "standard_model": "claude-sonnet-4-6",
            },
            "openai": {
                "api_key": "test-openai-key",
                "fast_model": "gpt-4o-mini",
                "standard_model": "gpt-4o",
            },
            "daily_token_budget": 500000,
        }
    }

@pytest.mark.asyncio
async def test_llm_client_calls_primary(llm_config):
    client = LLMClient(llm_config)
    with patch.object(client, "_call_claude", new_callable=AsyncMock) as mock:
        mock.return_value = "response text"
        result = await client.complete("test prompt", model_tier="fast")
        assert result == "response text"
        mock.assert_called_once_with("test prompt", "fast")

@pytest.mark.asyncio
async def test_llm_client_falls_back_on_error(llm_config):
    client = LLMClient(llm_config)
    with patch.object(client, "_call_claude", new_callable=AsyncMock) as mock_claude, \
         patch.object(client, "_call_openai", new_callable=AsyncMock) as mock_openai:
        mock_claude.side_effect = Exception("Claude down")
        mock_openai.return_value = "fallback response"
        result = await client.complete("test prompt", model_tier="fast")
        assert result == "fallback response"

@pytest.mark.asyncio
async def test_llm_dry_run_returns_mock(llm_config):
    client = LLMClient(llm_config, dry_run=True)
    result = await client.complete("test prompt")
    assert "[DRY RUN]" in result

@pytest.mark.asyncio
async def test_llm_client_budget_exhausted(llm_config):
    client = LLMClient(llm_config)
    client._tokens_used = client._token_budget
    with pytest.raises(RuntimeError, match="budget exhausted"):
        await client.complete("test prompt")

@pytest.mark.asyncio
async def test_llm_client_both_providers_fail(llm_config):
    client = LLMClient(llm_config)
    with patch.object(client, "_call_claude", new_callable=AsyncMock) as mock_claude, \
         patch.object(client, "_call_openai", new_callable=AsyncMock) as mock_openai:
        mock_claude.side_effect = Exception("Claude down")
        mock_openai.side_effect = Exception("OpenAI down")
        with pytest.raises(RuntimeError, match="Both providers failed"):
            await client.complete("test prompt")
