# src/config.py
import os
import re
from pathlib import Path

import yaml

# Auto-load .env from project root
_env_file = Path(__file__).parent.parent / "config" / ".env"
if _env_file.exists():
    with open(_env_file, encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#"):
                continue
            if "=" in _line:
                _key, _, _val = _line.partition("=")
                _key = _key.strip()
                _val = _val.strip()
                if _key and _key not in os.environ:
                    os.environ[_key] = _val

class ConfigError(Exception):
    pass

def _interpolate_env_vars(value, skip_missing: bool = False):
    """Replace ${VAR_NAME} with environment variable values.
    If skip_missing=True, leave ${VAR} as-is instead of raising."""
    if isinstance(value, str):
        def replacer(match):
            var_name = match.group(1)
            env_val = os.environ.get(var_name)
            if env_val is None:
                if skip_missing:
                    return match.group(0)  # leave as-is
                raise ConfigError(
                    f"Environment variable '{var_name}' is not set. "
                    f"Required by config. Set it or add to .env file."
                )
            return env_val
        return re.sub(r'\$\{(\w+)\}', replacer, value)
    elif isinstance(value, dict):
        return {k: _interpolate_env_vars(v, skip_missing) for k, v in value.items()}
    elif isinstance(value, list):
        return [_interpolate_env_vars(item, skip_missing) for item in value]
    return value

def load_config(config_path: str, strict: bool = True) -> dict:
    """Load YAML config with environment variable interpolation.
    If strict=False, missing env vars for disabled channels are tolerated."""
    with open(config_path, "r", encoding="utf-8") as f:
        raw_config = yaml.safe_load(f)
    return _interpolate_env_vars(raw_config, skip_missing=not strict)

def load_feeds(feeds_path: str) -> dict:
    """Load feeds.yaml (no env var interpolation needed)."""
    with open(feeds_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)
