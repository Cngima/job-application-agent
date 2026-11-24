"""Load config.yaml into validated pydantic models."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class AIConfig(BaseModel):
    provider: str = "claude_code"
    max_calls_per_run: int = 60
    batch_size: int = 5
    timeout_seconds: int = 180
    claude_code: dict = Field(default_factory=dict)
    ollama: dict = Field(default_factory=dict)
    anthropic_api: dict = Field(default_factory=dict)


class RoutingConfig(BaseModel):
    evergreen_days: int = 60


class FitConfig(BaseModel):
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    top_k: int = 4
    shortlist_threshold: int = 70


class HttpConfig(BaseModel):
    user_agent: str
    min_seconds_between_requests: float = 2.0
    timeout_seconds: float = 20


class Config(BaseModel):
    profile: str = "sample"
    data_dir: Path = Path("data/sample")
    ai: AIConfig = Field(default_factory=AIConfig)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    fit: FitConfig = Field(default_factory=FitConfig)
    http: HttpConfig


def load_config(path: Path | str = "config.yaml") -> Config:
    with open(path, encoding="utf-8") as fh:
        return Config.model_validate(yaml.safe_load(fh))
