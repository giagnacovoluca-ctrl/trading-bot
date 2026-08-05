"""Application settings — loaded from environment variables and config/default.yaml."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "extra": "ignore", "env_ignore_empty": True}

    github_token: str = ""
    algora_api_key: str = ""
    anthropic_api_key: str = ""
    groq_api_key: str = ""

    # BB_ overrides (opzionali, per chi vuole namespacare)
    bb_log_level: str = ""
    bb_db_url: str = ""
    bb_env: str = ""

    @property
    def log_level(self) -> str:
        return self.bb_log_level or "INFO"

    @property
    def db_url(self) -> str:
        return self.bb_db_url or "sqlite:///storage/db/bountybrain.db"

    @property
    def env(self) -> str:
        return self.bb_env or "development"
    env: str = "development"
    log_level: str = "INFO"
    db_url: str = "sqlite:///storage/db/bountybrain.db"

    # Private: YAML config loaded in post-init
    _yaml_config: dict = {}

    def model_post_init(self, __context: Any) -> None:
        config_path = Path("config/default.yaml")
        if config_path.exists():
            with open(config_path) as f:
                object.__setattr__(self, "_yaml_config", yaml.safe_load(f) or {})

    def get(self, key: str, default=None):
        """Dot-notation access to YAML config.

        Example: settings.get('scout.run_interval_minutes')
        """
        keys = key.split(".")
        val = self._yaml_config
        for k in keys:
            if isinstance(val, dict):
                val = val.get(k)
            else:
                return default
        return val if val is not None else default


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """Reset singleton — useful in tests."""
    global _settings
    _settings = None
