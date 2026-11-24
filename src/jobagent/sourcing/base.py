"""Shared interface for source adapters."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from jobagent.sourcing.config import SourceConfig
from jobagent.sourcing.http import PoliteClient
from jobagent.sourcing.models import Job

log = logging.getLogger(__name__)


@dataclass
class SourceStats:
    listed: int = 0  # postings seen on the source
    title_filtered: int = 0  # dropped by the title prefilter before any detail fetch
    detail_fetches: int = 0
    known_skipped: int = 0  # already stored or seen this run: no request made
    errors: list[str] = field(default_factory=list)


@dataclass
class SourcingContext:
    title_ok: Callable[[str], bool]
    max_detail_fetches: int = 60  # per source per run, protects large agency boards
    # True if this posting URL is already stored or was seen earlier this run.
    known: Callable[[str], bool] = lambda url: False


class Source(ABC):
    """An adapter turns one configured source into normalised Jobs.

    Adapters must apply `ctx.title_ok` before any per-job detail request, so a board with
    thousands of postings costs one list request plus a handful of detail requests.
    """

    def __init__(self, cfg: SourceConfig, client: PoliteClient, ctx: SourcingContext) -> None:
        self.cfg = cfg
        self.client = client
        self.ctx = ctx
        self.stats = SourceStats()

    @property
    def respect_robots(self) -> bool:
        return self.cfg.robots_override_reason is None

    def _get_json(self, url: str, **kw):
        return self.client.get_json(url, respect_robots=self.respect_robots, **kw)

    def _get_text(self, url: str, **kw) -> str:
        r = self.client.get(url, respect_robots=self.respect_robots, **kw)
        r.raise_for_status()
        return r.text

    def _keep(self, title: str) -> bool:
        self.stats.listed += 1
        if self.ctx.title_ok(title):
            return True
        self.stats.title_filtered += 1
        return False

    def _detail_gate(self, url: str) -> str:
        """Decide before a per-job detail request: "skip" a posting we already have,
        "stop" when the per-run cap is reached, otherwise "fetch".

        Checking first matters: Pavago lists the same posting once per location, and
        re-fetching stored jobs would spend the whole cap before reaching new ones.
        """
        if self.ctx.known(url):
            self.stats.known_skipped += 1
            return "skip"
        if self.stats.detail_fetches >= self.ctx.max_detail_fetches:
            self.stats.errors.append("detail fetch cap reached; remaining jobs skipped")
            return "stop"
        self.stats.detail_fetches += 1
        return "fetch"

    @abstractmethod
    def fetch(self) -> Iterator[Job]:
        """Yield jobs that passed the title prefilter, with descriptions where available."""
