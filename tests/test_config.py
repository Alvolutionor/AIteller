# tests/test_config.py
import pytest
from src.config import load_config, ConfigError

def test_load_config_interpolates_env_vars(tmp_path, monkeypatch):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        'llm:\n  claude:\n    api_key: "${TEST_API_KEY}"\n'
    )
    monkeypatch.setenv("TEST_API_KEY", "sk-test-123")
    config = load_config(str(config_file))
    assert config["llm"]["claude"]["api_key"] == "sk-test-123"

def test_load_config_raises_on_missing_env_var(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        'llm:\n  claude:\n    api_key: "${NONEXISTENT_VAR_XYZ}"\n'
    )
    with pytest.raises(ConfigError, match="NONEXISTENT_VAR_XYZ"):
        load_config(str(config_file))

def test_load_config_preserves_non_env_values(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        'collection:\n  interval_hours: 3\n  lookback_hours: 6\n'
    )
    config = load_config(str(config_file))
    assert config["collection"]["interval_hours"] == 3
