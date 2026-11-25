"""One interface for every AI call: `complete(system, prompt, json_schema) -> validated JSON`.

Implementations (chosen in config.yaml `ai.provider`):
- claude_code   (default) Claude Code CLI in print mode, using the owner's subscription login.
- ollama        a local model on localhost:11434 (free fallback).
- anthropic_api the Anthropic Python SDK with ANTHROPIC_API_KEY (off by default; paid).
- fake          recorded responses or a deterministic function, for CI and the public demo.

`AIRunner` wraps a provider with the things every call needs: a per-run call cap, one retry on
invalid output, pydantic validation, and a row in the ai_calls table for every attempt.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from jobagent.db import now_iso

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class AIError(Exception):
    """A single call failed (timeout, bad exit code, network). The caller decides what next."""


class ProviderPaused(AIError):
    """Stop all AI work for this run: usage limit reached, provider unreachable, or the
    per-run call cap is spent. The run continues with rule-only results."""


class InvalidOutput(AIError):
    """The model's output did not match the schema, even after one retry."""


@dataclass
class AIResult:
    data: dict
    provider: str
    model: str | None
    latency_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


class Provider(ABC):
    name: str = ""
    model: str | None = None

    @abstractmethod
    def complete(self, system: str, prompt: str, schema: dict) -> AIResult:
        """Return JSON matching `schema` (the runner validates it again)."""


class AIRunner:
    def __init__(
        self,
        provider: Provider,
        conn: sqlite3.Connection | None = None,
        max_calls: int = 60,
        prompt_version: str | None = None,
    ) -> None:
        self.provider = provider
        self.conn = conn
        self.max_calls = max_calls
        self.prompt_version = prompt_version
        self.calls = 0
        self.paused_reason: str | None = None

    def _log(
        self,
        purpose: str,
        items: int,
        attempt: int,
        status: str,
        res: AIResult | None = None,
        error: str | None = None,
        latency_ms: int | None = None,
    ) -> None:
        if self.conn is None:
            return
        self.conn.execute(
            "INSERT INTO ai_calls (started_at, purpose, provider, model, latency_ms, input_tokens, "
            "output_tokens, cost_usd, items, attempt, status, error, prompt_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                now_iso(),
                purpose,
                self.provider.name,
                (res.model if res else None) or self.provider.model,
                res.latency_ms if res else latency_ms,
                res.input_tokens if res else None,
                res.output_tokens if res else None,
                res.cost_usd if res else None,
                items,
                attempt,
                status,
                error,
                self.prompt_version,
            ),
        )
        self.conn.commit()

    def call(
        self,
        purpose: str,
        system: str,
        prompt: str,
        schema: dict,
        model_cls: type[T],
        items: int = 1,
    ) -> tuple[T, AIResult]:
        """Validated output, retrying once on invalid output.

        Raises ProviderPaused (stop AI for the run) or InvalidOutput.
        """
        if self.paused_reason:
            raise ProviderPaused(self.paused_reason)
        last_error = ""
        for attempt in (1, 2):
            if self.calls >= self.max_calls:
                self.paused_reason = f"AI call cap reached ({self.max_calls} per run)"
                raise ProviderPaused(self.paused_reason)
            self.calls += 1
            p = (
                prompt
                if attempt == 1
                else (
                    f"{prompt}\n\nYour previous answer was invalid ({last_error[:300]}). "
                    "Reply again with JSON that matches the schema exactly."
                )
            )
            started = time.monotonic()
            try:
                res = self.provider.complete(system, p, schema)
            except ProviderPaused as e:
                self.paused_reason = str(e)
                self._log(
                    purpose,
                    items,
                    attempt,
                    "paused",
                    error=str(e),
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
                raise
            except AIError as e:
                self._log(
                    purpose,
                    items,
                    attempt,
                    "error",
                    error=str(e),
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
                last_error = str(e)
                if attempt == 2:
                    raise
                continue
            try:
                parsed = model_cls.model_validate(res.data)
            except ValidationError as e:
                last_error = str(e).replace("\n", " ")
                self._log(purpose, items, attempt, "invalid", res=res, error=last_error[:500])
                continue
            self._log(purpose, items, attempt, "ok", res=res)
            return parsed, res
        raise InvalidOutput(f"invalid output after retry: {last_error[:300]}")


def extract_json(text: str) -> dict:
    """Parse JSON from a model's text reply, tolerating a code fence around it."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end == -1:
        raise AIError("no JSON object in reply")
    try:
        return json.loads(t[start : end + 1])
    except json.JSONDecodeError as e:
        raise AIError(f"reply is not valid JSON: {e}") from e
