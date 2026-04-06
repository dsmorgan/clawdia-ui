"""Application configuration loaded from environment variables and YAML files."""

import os
from pathlib import Path
from functools import lru_cache

import yaml
from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    """Application settings from environment variables."""

    secret_key: str = Field(default="change-me-in-production")
    debug: bool = Field(default=False)
    allowed_hosts: str = Field(default="*")
    database_path: str = Field(default="/app/data/clawdia.db")
    quick_actions_path: str = Field(default="/app/config/quick_actions.yaml")

    # OpenClaw gateway
    openclaw_host: str = Field(default="10.11.24.52")
    openclaw_port: int = Field(default=18789)
    openclaw_gateway_token: str = Field(default="")
    openclaw_agent_id: str = Field(default="main")

    # OIDC / Authentik
    oidc_client_id: str = Field(default="")
    oidc_client_secret: str = Field(default="")
    oidc_issuer: str = Field(default="https://auth.yourdomain.com/application/o/clawdia-ui/")
    login_redirect_url: str = Field(default="/")
    logout_redirect_url: str = Field(default="/logged-out")

    @property
    def openclaw_ws_url(self) -> str:
        return f"ws://{self.openclaw_host}:{self.openclaw_port}/ws"

    @property
    def database_dir(self) -> Path:
        return Path(self.database_path).parent

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


@lru_cache()
def get_settings() -> Settings:
    return Settings()


def load_quick_actions() -> list[dict]:
    """Load quick actions from YAML file. Re-reads on every call so edits
    take effect without restart. Returns empty list if file missing or invalid."""
    path = get_settings().quick_actions_path
    try:
        with open(path, "r") as f:
            data = yaml.safe_load(f)
        if data and "quick_actions" in data:
            actions = data["quick_actions"]
            if isinstance(actions, list):
                return [
                    a for a in actions
                    if isinstance(a, dict) and "label" in a and "prompt" in a
                ]
        return []
    except (FileNotFoundError, yaml.YAMLError, OSError):
        return []
