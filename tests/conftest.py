"""Shared test helpers: a PoliteClient backed by an in-memory HTTP router (no network)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from jobagent.sourcing.http import PoliteClient

UA = "job-application-agent/0.1 (+https://github.com/Cngima/job-application-agent)"

Handler = Callable[[httpx.Request], httpx.Response] | dict | list | str | httpx.Response


class Router:
    """Map 'host/path' (query ignored) to a response. robots.txt defaults to 404 (allow all)."""

    def __init__(self, routes: dict[str, Handler] | None = None) -> None:
        self.routes: dict[str, Handler] = dict(routes or {})
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        key = f"{request.url.host}{request.url.path}"
        h = self.routes.get(key)
        if h is None:
            return httpx.Response(404, text="not found")
        if callable(h) and not isinstance(h, httpx.Response):
            return h(request)
        if isinstance(h, httpx.Response):
            return h
        if isinstance(h, dict | list):
            return httpx.Response(200, json=h)
        return httpx.Response(200, text=h)

    def paths(self) -> list[str]:
        return [f"{r.url.host}{r.url.path}" for r in self.calls]


@pytest.fixture
def make_client() -> Callable[..., tuple[PoliteClient, Router]]:
    def _make(routes: dict[str, Any] | None = None, **kw) -> tuple[PoliteClient, Router]:
        router = Router(routes)
        sleeps: list[float] = []
        client = PoliteClient(
            UA,
            min_interval=kw.pop("min_interval", 0.0),
            transport=httpx.MockTransport(router),
            sleep=sleeps.append,
            **kw,
        )
        client.sleeps = sleeps  # type: ignore[attr-defined]
        return client, router

    return _make


def jdump(obj: Any) -> str:
    return json.dumps(obj)
