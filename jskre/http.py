"""Polite HTTP client for jskre.com.

Design goals, in order:

1. Never fetch a path the site's robots.txt disallows. The rules are fetched
   once per run and consulted for every URL. Note that jskre.com disallows
   ``/property/`` and ``/property-id/`` (legacy routes) but *not*
   ``/properties/``, which is where live listing pages actually are.
2. Stay well under one request per second, with jitter, so the crawl is
   invisible in the site's load graphs.
3. Retry only on transient failures, with exponential backoff, and give up
   rather than hammer.
"""

from __future__ import annotations

import logging
import random
import time
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import requests

log = logging.getLogger(__name__)

BASE_URL = "https://www.jskre.com"

# Transient statuses worth a retry. 403/404 are not: they mean "no".
RETRY_STATUSES = {429, 500, 502, 503, 504, 520, 522, 524}


class RobotsDisallowed(Exception):
    """Raised when a URL is blocked by robots.txt."""


@dataclass
class FetchStats:
    requests: int = 0
    retries: int = 0
    blocked: int = 0
    failed: int = 0


class PoliteClient:
    """A rate-limited, robots-respecting requests wrapper."""

    def __init__(
        self,
        base_url: str = BASE_URL,
        user_agent: str = "jskre-deal-finder/1.0 (personal market research)",
        delay: float = 1.5,
        jitter: float = 0.75,
        timeout: float = 30.0,
        max_retries: int = 4,
        respect_robots: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        self.delay = delay
        self.jitter = jitter
        self.timeout = timeout
        self.max_retries = max_retries
        self.respect_robots = respect_robots
        self.stats = FetchStats()

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                # Ask for the plain English pages; ?lang= query params are
                # robots-disallowed so we signal preference via headers only.
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml",
            }
        )

        self._robots: urllib.robotparser.RobotFileParser | None = None
        self._last_request = 0.0

    # ---------------------------------------------------------------- robots

    def _load_robots(self) -> urllib.robotparser.RobotFileParser:
        if self._robots is None:
            rp = urllib.robotparser.RobotFileParser()
            robots_url = f"{self.base_url}/robots.txt"
            try:
                resp = self.session.get(robots_url, timeout=self.timeout)
                resp.raise_for_status()
                rp.parse(resp.text.splitlines())
                log.info("Loaded robots.txt from %s", robots_url)
            except requests.RequestException as exc:
                # Fail closed on the paths we care about rather than assuming
                # everything is permitted.
                log.warning("Could not read robots.txt (%s); assuming crawl-deny", exc)
                rp.parse(["User-agent: *", "Disallow: /"])
            self._robots = rp
        return self._robots

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        rp = self._load_robots()
        # Check against our own UA and the wildcard group. robotparser already
        # falls back to '*' when the specific agent has no group.
        return rp.can_fetch(self.user_agent, url)

    # ----------------------------------------------------------------- fetch

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        wait = self.delay + random.uniform(0, self.jitter) - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def get(self, path_or_url: str) -> str | None:
        """Fetch a page and return its HTML, or None if permanently unavailable.

        Raises RobotsDisallowed if the URL is blocked -- that is a programming
        error in the caller, not a runtime condition to swallow.
        """
        url = urljoin(self.base_url + "/", path_or_url.lstrip("/"))

        if urlparse(url).netloc != urlparse(self.base_url).netloc:
            raise ValueError(f"Refusing to fetch off-site URL: {url}")

        if not self.allowed(url):
            self.stats.blocked += 1
            raise RobotsDisallowed(f"robots.txt disallows {url}")

        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                resp = self.session.get(url, timeout=self.timeout)
                self.stats.requests += 1
            except requests.RequestException as exc:
                if attempt == self.max_retries:
                    log.error("Giving up on %s: %s", url, exc)
                    self.stats.failed += 1
                    return None
                backoff = 2**attempt
                log.warning("Network error on %s (%s); retry in %ss", url, exc, backoff)
                self.stats.retries += 1
                time.sleep(backoff)
                continue

            if resp.status_code == 200:
                return resp.text

            if resp.status_code == 404:
                log.info("404 for %s (listing probably removed)", url)
                return None

            if resp.status_code in RETRY_STATUSES and attempt < self.max_retries:
                # Honour Retry-After when the server sends one.
                retry_after = resp.headers.get("Retry-After")
                backoff = 2**attempt
                if retry_after and retry_after.isdigit():
                    backoff = max(backoff, int(retry_after))
                log.warning(
                    "HTTP %s on %s; retry in %ss", resp.status_code, url, backoff
                )
                self.stats.retries += 1
                time.sleep(backoff)
                continue

            log.error("HTTP %s on %s; giving up", resp.status_code, url)
            self.stats.failed += 1
            return None

        return None
