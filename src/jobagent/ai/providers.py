"""Provider implementations. See ai/provider.py for the interface."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from jobagent.ai.provider import AIError, AIResult, Provider, ProviderPaused, extract_json

log = logging.getLogger(__name__)

USAGE_LIMIT = re.compile(
    r"usage limit|limit reached|rate[- ]limit|too many requests|quota|out of (?:extra )?usage|"
    r"5-hour limit|weekly limit|resets? at|credit balance|overloaded",
    re.I,
)


# --------------------------------------------------------------------------- Claude Code CLI


class ClaudeCodeProvider(Provider):
    """`claude -p` in print mode with structured output, using the owner's subscription login.

    The prompt goes in on stdin (no command-line length limit). The CLI runs with no tools,
    no MCP servers and no saved session, from a temporary folder, so it can only answer.
    """

    name = "claude_code"

    def __init__(
        self, model: str | None = None, timeout: int = 180, executable: str = "claude"
    ) -> None:
        self.model = model
        self.timeout = timeout
        self.executable = executable

    def _cmd(self, system: str, schema: dict) -> list[str]:
        exe = shutil.which(self.executable)
        if not exe:
            raise ProviderPaused(
                "Claude Code CLI not found on PATH (install it or switch provider)"
            )
        cmd = [
            exe,
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema),
            "--tools",
            "",
            "--no-session-persistence",
            "--strict-mcp-config",
            "--system-prompt",
            system,
        ]
        if self.model:
            cmd += ["--model", self.model]
        return cmd

    def complete(self, system: str, prompt: str, schema: dict) -> AIResult:
        started = time.monotonic()
        try:
            proc = subprocess.run(
                self._cmd(system, schema),
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                cwd=tempfile.gettempdir(),
            )
        except subprocess.TimeoutExpired as e:
            raise AIError(f"claude CLI timed out after {self.timeout}s") from e
        latency = int((time.monotonic() - started) * 1000)
        out, err = proc.stdout.strip(), proc.stderr.strip()
        try:
            payload = json.loads(out) if out else {}
        except json.JSONDecodeError:
            payload = {}
        text_blob = f"{payload.get('result', '')} {err} {out[:500] if not payload else ''}"
        if (
            proc.returncode != 0
            or payload.get("is_error")
            or payload.get("subtype", "success") != "success"
        ):
            if USAGE_LIMIT.search(text_blob) or payload.get("api_error_status") == 429:
                raise ProviderPaused(f"Claude usage limit: {text_blob.strip()[:200]}")
            raise AIError(f"claude CLI failed (exit {proc.returncode}): {text_blob.strip()[:300]}")
        data = payload.get("structured_output")
        if data is None:
            data = extract_json(payload.get("result", ""))
        usage = payload.get("usage") or {}
        model = next(iter(payload.get("modelUsage") or {}), self.model)
        return AIResult(
            data=data,
            provider=self.name,
            model=model,
            latency_ms=latency,
            input_tokens=(usage.get("input_tokens") or 0)
            + (usage.get("cache_creation_input_tokens") or 0)
            + (usage.get("cache_read_input_tokens") or 0)
            or None,
            output_tokens=usage.get("output_tokens"),
            cost_usd=None,  # subscription: no per-call cost
        )


# --------------------------------------------------------------------------- Ollama


class OllamaProvider(Provider):
    """Local model via Ollama's /api/chat with a JSON-schema `format` (free, offline)."""

    name = "ollama"

    def __init__(
        self,
        host: str = "http://localhost:11434",
        model: str = "qwen2.5:7b",
        timeout: int = 300,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.host = host.rstrip("/")
        self.model = model
        self.http = httpx.Client(timeout=timeout, transport=transport)

    def complete(self, system: str, prompt: str, schema: dict) -> AIResult:
        started = time.monotonic()
        try:
            r = self.http.post(
                f"{self.host}/api/chat",
                json={
                    "model": self.model,
                    "stream": False,
                    "format": schema,
                    "options": {"temperature": 0},
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                },
            )
        except httpx.TransportError as e:
            raise ProviderPaused(f"Ollama not reachable at {self.host} (is it running?)") from e
        if r.status_code == 404:
            raise ProviderPaused(
                f"Ollama model {self.model!r} not pulled (ollama pull {self.model})"
            )
        if r.status_code != 200:
            raise AIError(f"Ollama returned {r.status_code}: {r.text[:200]}")
        body = r.json()
        return AIResult(
            data=extract_json(body.get("message", {}).get("content", "")),
            provider=self.name,
            model=self.model,
            latency_ms=int((time.monotonic() - started) * 1000),
            input_tokens=body.get("prompt_eval_count"),
            output_tokens=body.get("eval_count"),
        )


# --------------------------------------------------------------------------- Anthropic API

# US$ per million tokens: (input, output, cache read). Cache writes bill at 1.25x input.
API_PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}


class AnthropicAPIProvider(Provider):
    """Paid API (off by default). Structured output via output_config.format, plus the
    server-side refusal fallback recommended for current models."""

    name = "anthropic_api"

    def __init__(
        self,
        model: str = "claude-opus-5-5",
        effort: str = "medium",
        max_tokens: int = 16000,
        client=None,
    ) -> None:
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self._client = client

    def _get_client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as e:
                raise ProviderPaused("install the API extra: pip install -e .[api]") from e
            if not os.getenv("ANTHROPIC_API_KEY"):
                raise ProviderPaused("ANTHROPIC_API_KEY is not set in .env")
            self._client = anthropic.Anthropic()
        return self._client

    def complete(self, system: str, prompt: str, schema: dict) -> AIResult:
        import anthropic

        client = self._get_client()
        started = time.monotonic()
        try:
            resp = client.beta.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config={
                    "effort": self.effort,
                    "format": {"type": "json_schema", "schema": schema},
                },
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except (
            anthropic.RateLimitError,
            anthropic.AuthenticationError,
            anthropic.PermissionDeniedError,
        ) as e:
            raise ProviderPaused(f"Anthropic API: {type(e).__name__}") from e
        except anthropic.APIStatusError as e:
            raise AIError(f"Anthropic API error {e.status_code}") from e
        except anthropic.APIConnectionError as e:
            raise AIError("Anthropic API connection error") from e
        if resp.stop_reason == "refusal":
            raise AIError("model declined the request")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        u = resp.usage
        cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(u, "cache_creation_input_tokens", 0) or 0
        price = API_PRICES.get(resp.model or self.model)
        cost = None
        if price:
            cost = round(
                (
                    u.input_tokens * price[0]
                    + cache_write * price[0] * 1.25
                    + cache_read * price[2]
                    + u.output_tokens * price[1]
                )
                / 1_000_000,
                6,
            )
        return AIResult(
            data=extract_json(text),
            provider=self.name,
            model=resp.model or self.model,
            latency_ms=int((time.monotonic() - started) * 1000),
            input_tokens=u.input_tokens + cache_read + cache_write,
            output_tokens=u.output_tokens,
            cost_usd=cost,
        )


# --------------------------------------------------------------------------- fake (CI, demo)


def prompt_key(system: str, prompt: str) -> str:
    return hashlib.sha1(f"{system}\n---\n{prompt}".encode()).hexdigest()[:16]


class FakeProvider(Provider):
    """Replays recorded responses keyed by a hash of (system, prompt); falls back to a
    deterministic function when given one. Never touches the network."""

    name = "fake"
    model = "fake"

    def __init__(
        self,
        recordings: dict[str, dict] | Path | None = None,
        fallback: Callable[[str, str, dict], dict] | None = None,
    ) -> None:
        if isinstance(recordings, Path):
            recordings = (
                json.loads(recordings.read_text(encoding="utf-8")) if recordings.exists() else {}
            )
        self.recordings = recordings or {}
        self.fallback = fallback
        self.prompts: list[str] = []

    def complete(self, system: str, prompt: str, schema: dict) -> AIResult:
        self.prompts.append(prompt)
        key = prompt_key(system, prompt)
        if key in self.recordings:
            data = self.recordings[key]
        elif self.fallback:
            data = self.fallback(system, prompt, schema)
        else:
            raise AIError(f"no recorded response for prompt {key}")
        return AIResult(data=data, provider=self.name, model=self.model, latency_ms=0)


def build_provider(ai_cfg, name: str | None = None, fake_fallback=None) -> Provider:
    """Create the configured provider (config.yaml `ai`)."""
    name = name or ai_cfg.provider
    if name == "claude_code":
        return ClaudeCodeProvider(ai_cfg.claude_code.get("model"), ai_cfg.timeout_seconds)
    if name == "ollama":
        return OllamaProvider(
            ai_cfg.ollama.get("host", "http://localhost:11434"),
            ai_cfg.ollama.get("model", "qwen2.5:7b"),
            ai_cfg.timeout_seconds,
        )
    if name == "anthropic_api":
        return AnthropicAPIProvider(
            ai_cfg.anthropic_api.get("model", "claude-opus-5-5"),
            ai_cfg.anthropic_api.get("effort", "medium"),
        )
    if name == "fake":
        return FakeProvider(fallback=fake_fallback)
    raise ValueError(f"unknown AI provider {name!r}")
