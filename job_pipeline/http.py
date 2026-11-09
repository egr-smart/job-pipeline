"""Shared HTTP client with polite per-host rate limiting and a single retry on transient errors."""

import time
from urllib.parse import urlparse

import httpx

USER_AGENT = "job-pipeline/0.1 (personal job search tool)"


class Http:
    def __init__(self, min_interval: float = 0.5, timeout: float = 30):
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT}, timeout=timeout, follow_redirects=True
        )
        self._min_interval = min_interval
        self._last_hit: dict[str, float] = {}

    def get(self, url: str, **kwargs) -> httpx.Response:
        host = urlparse(url).netloc
        for attempt in range(2):
            wait = self._last_hit.get(host, 0) + self._min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_hit[host] = time.monotonic()
            try:
                resp = self._client.get(url, **kwargs)
            except httpx.TransportError:
                if attempt:
                    raise
                continue
            if resp.status_code in (429, 500, 502, 503, 504) and not attempt:
                time.sleep(2)
                continue
            return resp
        raise AssertionError("unreachable")

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "Http":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
