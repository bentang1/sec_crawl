"""Rate-limited, retrying HTTP client for sec.gov / data.sec.gov."""

from __future__ import annotations

import logging

import requests
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from .config import Config
from .throttle import RateLimiter

logger = logging.getLogger(__name__)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, requests.exceptions.RequestException):
        response = getattr(exc, "response", None)
        if response is not None and response.status_code not in (429, 500, 502, 503, 504):
            return False
        return True
    return False


class EdgarClient:
    """Wraps a requests.Session with the mandatory SEC User-Agent, a shared
    ~N req/s throttle, and retry-with-backoff on transient errors / 429s.
    """

    def __init__(self, config: Config):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": config.user_agent,
                "Accept-Encoding": "gzip, deflate",
            }
        )
        self._limiter = RateLimiter(rate=config.requests_per_second)

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        reraise=True,
    )
    def get(self, url: str, **kwargs) -> requests.Response:
        self._limiter.acquire()
        response = self.session.get(url, timeout=30, **kwargs)
        if response.status_code == 429:
            logger.warning("429 rate-limited on %s, retrying with backoff", url)
        response.raise_for_status()
        return response
