"""A polite HTTP client: robots.txt, per-host rate limit, identifying User-Agent, retries.

Every network read in the sourcing layer goes through this class, so the politeness rules are
enforced in one place and tested once.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}


class RobotsDisallowed(Exception):
    """robots.txt for this host does not allow our User-Agent to fetch the URL."""


class HostUnavailable(Exception):
    """robots.txt could not be read (5xx or network error), so the host is skipped this run."""


class PoliteClient:
    def __init__(
        self,
        user_agent: str,
        min_interval: float = 2.0,
        timeout: float = 20.0,
        max_attempts: int = 3,
        max_retry_after: float = 60.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.user_agent = user_agent
        self.min_interval = min_interval
        self.max_attempts = max_attempts
        self.max_retry_after = max_retry_after
        self._sleep = sleep
        self._clock = clock
        self._last: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}
        self._unavailable: set[str] = set()
        self.requests_made = 0
        self._http = httpx.Client(
            headers={"User-Agent": user_agent, "Accept": "application/json, text/html;q=0.9"},
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
        )

    # ---- politeness

    def _wait_turn(self, host: str) -> None:
        last = self._last.get(host)
        if last is not None:
            gap = self.min_interval - (self._clock() - last)
            if gap > 0:
                self._sleep(gap)
        self._last[host] = self._clock()

    def _robots_for(self, scheme: str, host: str) -> RobotFileParser | None:
        """Parsed robots.txt, or None meaning 'allow all' (RFC 9309: 4xx = no rules)."""
        key = f"{scheme}://{host}"
        if key in self._unavailable:
            raise HostUnavailable(f"{key}: robots.txt could not be read earlier this run")
        if key in self._robots:
            return self._robots[key]
        self._wait_turn(host)
        self.requests_made += 1
        try:
            r = self._http.get(f"{key}/robots.txt")
        except httpx.TransportError as e:
            self._unavailable.add(key)
            raise HostUnavailable(f"{key}: robots.txt unreachable ({type(e).__name__})") from e
        if r.status_code >= 500:
            self._unavailable.add(key)
            raise HostUnavailable(f"{key}: robots.txt returned {r.status_code}")
        if r.status_code >= 400:
            self._robots[key] = None
            return None
        rp = RobotFileParser()
        rp.parse(r.text.splitlines())
        self._robots[key] = rp
        return rp

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        rp = self._robots_for(parts.scheme, parts.netloc)
        return True if rp is None else rp.can_fetch(self.user_agent, url)

    # ---- requests

    def request(
        self,
        method: str,
        url: str,
        *,
        respect_robots: bool = True,
        **kwargs,
    ) -> httpx.Response:
        """Send a request after robots and rate-limit checks, retrying transient failures.

        `respect_robots=False` exists only for endpoints whose robots.txt the owner has
        explicitly decided does not apply (recorded with a reason in config/sources.yaml).
        """
        parts = urlsplit(url)
        if respect_robots and not self.allowed(url):
            raise RobotsDisallowed(url)
        last_exc: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            self._wait_turn(parts.netloc)
            self.requests_made += 1
            try:
                r = self._http.request(method, url, **kwargs)
            except httpx.TransportError as e:
                last_exc = e
                log.warning("%s %s failed (%s), attempt %d", method, url, type(e).__name__, attempt)
                self._backoff(attempt, None)
                continue
            if r.status_code in RETRY_STATUSES and attempt < self.max_attempts:
                log.warning("%s %s returned %d, attempt %d", method, url, r.status_code, attempt)
                self._backoff(attempt, r.headers.get("Retry-After"))
                continue
            return r
        assert last_exc is not None
        raise last_exc

    def get(self, url: str, **kwargs) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def get_json(self, url: str, **kwargs):
        r = self.get(url, **kwargs)
        r.raise_for_status()
        return r.json()

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        delay = 2.0**attempt
        if retry_after and retry_after.replace(".", "", 1).isdigit():
            delay = float(retry_after)  # seconds form; HTTP-date form falls back to backoff
        self._sleep(min(delay, self.max_retry_after))

    def close(self) -> None:
        self._http.close()
