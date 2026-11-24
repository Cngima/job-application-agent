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


class SourcingConfig(BaseModel):
    max_detail_fetches_per_source: int = 60
    max_new_companies_per_run: int = 15


class GmailConfig(BaseModel):
    enabled: bool = False
    client_secret: Path = Path("data/private/gmail_client_secret.json")
    token: Path = Path("data/private/gmail_token.json")
    lookback_days: int = 7
    max_messages_per_run: int = 50
    linkedin_senders: list[str] = Field(
        default_factory=lambda: ["jobalerts-noreply@linkedin.com", "jobs-noreply@linkedin.com"]
    )


class IntakeConfig(BaseModel):
    inbox_dir: Path = Path("inbox")
    follow_links_per_post: int = 2
    gmail: GmailConfig = Field(default_factory=GmailConfig)


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
    sourcing: SourcingConfig = Field(default_factory=SourcingConfig)
    intake: IntakeConfig = Field(default_factory=IntakeConfig)
    http: HttpConfig

    def with_profile(self, profile: str | None) -> Config:
        """Switch between the public sample data and Carol's private data."""
        if not profile:
            return self
        return self.model_copy(update={"profile": profile, "data_dir": Path("data") / profile})

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jobagent.db"


def load_config(path: Path | str = "config.yaml") -> Config:
    with open(path, encoding="utf-8") as fh:
        return Config.model_validate(yaml.safe_load(fh))
