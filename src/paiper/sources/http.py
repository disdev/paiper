"""Bounded stdlib HTTPS transport."""

import json
import math
import subprocess
import sys
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

MAX_BYTES = 2 * 1024 * 1024
TIMEOUT = 8
WORKER_TIMEOUT = 12
USER_AGENT = "Paiper/0.1 (personal e-paper dashboard; public-source cache)"


class SourceError(ValueError):
    def __init__(self, message, *, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


def fetch(url: str) -> bytes:
    """One isolated request, killed after 12 seconds even if DNS/TLS hangs.

    Four collector threads launch at most four short-lived workers. The child
    enforces body limits; the parent can always terminate a stuck system resolver
    without leaving hung executor threads behind.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "paiper.sources._worker", url],
            capture_output=True,
            timeout=WORKER_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise SourceError("HTTP worker deadline exceeded") from error
    except OSError as error:
        raise SourceError("Cannot start HTTP worker") from error
    if result.returncode != 0:
        try:
            detail = json.loads(result.stderr)
            message, retry_after = detail["error"], detail["retry_after"]
        except (ValueError, KeyError, TypeError):
            message, retry_after = "HTTP worker failed", None
        raise SourceError(message, retry_after=retry_after)
    if len(result.stdout) > MAX_BYTES:
        raise SourceError("Response exceeds 2 MiB")
    return result.stdout


def _request(url):
    """Fetch once; no retries, credentials, cookies or response decompression."""
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise SourceError("Only unauthenticated HTTPS sources are supported")
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, application/rss+xml, application/xml",
            "Accept-Encoding": "identity",
        },
    )
    deadline = time.monotonic() + TIMEOUT
    try:
        with urlopen(request, timeout=TIMEOUT) as response:
            if response.status != 200:
                raise SourceError(f"HTTP {response.status}")
            length = response.headers.get("Content-Length")
            if length is not None and int(length) > MAX_BYTES:
                raise SourceError("Response exceeds 2 MiB")
            if response.headers.get("Content-Encoding", "identity") != "identity":
                raise SourceError("Unsupported compressed response")
            chunks, size = [], 0
            while True:
                if time.monotonic() >= deadline:
                    raise SourceError("Response deadline exceeded")
                chunk = response.read1(min(65536, MAX_BYTES + 1 - size))
                if not chunk:
                    return b"".join(chunks)
                size += len(chunk)
                if size > MAX_BYTES:
                    raise SourceError("Response exceeds 2 MiB")
                chunks.append(chunk)
    except HTTPError as error:
        delay = (
            _retry_after(error.headers.get("Retry-After")) if error.headers else None
        )
        raise SourceError(f"HTTP {error.code}", retry_after=delay) from error
    except (URLError, OSError, ValueError, HTTPException) as error:
        if isinstance(error, SourceError):
            raise
        raise SourceError(f"HTTP transport failed ({type(error).__name__})") from error


def _retry_after(value):
    if value is None:
        return None
    try:
        try:
            delay = float(value)
        except ValueError:
            delay = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
        return min(max(delay, 1), 21600) if math.isfinite(delay) else None
    except (ValueError, TypeError, OverflowError):
        return None
