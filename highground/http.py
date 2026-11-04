"""One HTTP client for all upstream calls: timeouts, retries with backoff + jitter, a clear User-Agent."""

import random
import time

import httpx

USER_AGENT = "HighGround-SF/0.1 (+https://github.com/shokhabbos-mukhammatov/-Hack-Night-with-Mistral-AI-Elastic-Search)"
RETRY_STATUS = {429, 500, 502, 503, 504}


def client(timeout: float = 20.0) -> httpx.Client:
    return httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT}, follow_redirects=True)


def request(
    http: httpx.Client,
    method: str,
    url: str,
    *,
    retries: int = 2,
    backoff_s: float = 1.0,
    **kwargs,
) -> httpx.Response:
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = http.request(method, url, **kwargs)
            if resp.status_code in RETRY_STATUS and attempt < retries:
                _sleep(backoff_s, attempt, resp.headers.get("retry-after"))
                continue
            resp.raise_for_status()
            return resp
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            last_exc = exc
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code not in RETRY_STATUS:
                raise
            if attempt < retries:
                _sleep(backoff_s, attempt, None)
    assert last_exc is not None
    raise last_exc


def get_json(http: httpx.Client, url: str, **kwargs):
    return request(http, "GET", url, **kwargs).json()


def _sleep(base: float, attempt: int, retry_after: str | None) -> None:
    if retry_after and retry_after.isdigit():
        delay = min(float(retry_after), 30.0)
    else:
        delay = base * (2**attempt) + random.uniform(0, base / 2)
    time.sleep(delay)
