"""Small stdlib-only HTTP client with rate limiting and retries.

Bangumi asks for a descriptive ``User-Agent`` and is a shared community
service, so every client here throttles by default and retries transient
failures with exponential backoff.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .log import get_logger

log = get_logger("http")


class HttpError(Exception):
    """Base class for transport level failures."""


class NetworkError(HttpError):
    def __init__(self, url: str, message: str) -> None:
        super().__init__("network error for %s: %s" % (url, message))
        self.url = url
        self.message = message


class ApiError(HttpError):
    def __init__(
        self,
        status: int,
        url: str,
        message: str,
        retry_after: float | None = None,
    ) -> None:
        super().__init__("HTTP %d for %s: %s" % (status, url, message))
        self.status = status
        self.url = url
        self.message = message
        self.retry_after = retry_after


class Response:
    __slots__ = ("status", "headers", "body", "url")

    def __init__(self, status: int, headers: dict[str, str], body: bytes, url: str) -> None:
        self.status = status
        self.headers = headers
        self.body = body
        self.url = url

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self) -> object:
        if not self.body:
            return None
        return json.loads(self.body.decode("utf-8"))


class HttpClient:
    def __init__(
        self,
        user_agent: str,
        timeout: float = 20.0,
        max_retries: int = 3,
        min_interval: float = 0.0,
    ) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self.min_interval = max(0.0, float(min_interval))
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    # ------------------------------------------------------------------ rate
    def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_allowed = now + self.min_interval

    def _backoff(self, attempt: int, retry_after: float | None) -> None:
        delay = retry_after if retry_after and retry_after > 0 else min(30.0, 2.0 ** attempt)
        log.debug("retrying in %.1fs (attempt %d)", delay, attempt + 1)
        time.sleep(delay)

    # --------------------------------------------------------------- request
    def request(
        self,
        method: str,
        url: str,
        *,
        json_body: object | None = None,
        form_body: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        max_retries: int | None = None,
    ) -> Response:
        retries = self.max_retries if max_retries is None else max_retries
        hdrs = {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        }
        data: bytes | None = None
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        elif form_body is not None:
            data = urllib.parse.urlencode(form_body).encode("utf-8")
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        if headers:
            hdrs.update(headers)

        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            self._throttle()
            req = urllib.request.Request(url, data=data, headers=hdrs, method=method.upper())
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return Response(resp.status, dict(resp.headers), resp.read(), url)
            except urllib.error.HTTPError as exc:
                body = b""
                try:
                    body = exc.read()
                except Exception:  # pragma: no cover - defensive
                    pass
                message = body.decode("utf-8", "replace")[:400] or exc.reason
                retry_after = None
                raw_retry = (exc.headers or {}).get("Retry-After")
                if raw_retry:
                    try:
                        retry_after = float(raw_retry)
                    except ValueError:
                        retry_after = None
                err = ApiError(exc.code, url, str(message), retry_after)
                if exc.code in (408, 425, 429, 500, 502, 503, 504) and attempt < retries:
                    self._backoff(attempt, retry_after)
                    last_exc = err
                    continue
                raise err from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                reason = getattr(exc, "reason", exc)
                err = NetworkError(url, str(reason))
                if attempt < retries:
                    self._backoff(attempt, None)
                    last_exc = err
                    continue
                raise err from None
        # pragma: no cover - loop always returns or raises
        raise last_exc or NetworkError(url, "exhausted retries")
