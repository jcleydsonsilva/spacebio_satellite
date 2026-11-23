"""The only code that talks to the public databases, and the rules it obeys.

* It contacts ONLY the hosts the task lists as allowed (and only https, except for this computer when developing).
* It waits ``min_interval`` between two requests to the same host.
* It follows a redirect only if the new address is allowed too; it never sends a SpaceBio token (it does not know it).
* It refuses answers larger than a fixed size, and treats 429 and 5xx as "this database is busy": the task is given back.
* Your own API keys (NCBI_API_KEY, OPENALEX_API_KEY) are added from the environment to requests for THAT host only.
"""
from __future__ import annotations

import json
import os
import time
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests

from . import __version__
from .config import LOOPBACK
from .errors import HostNotAllowed, SourceError

MAX_BYTES = 25_000_000
MAX_REDIRECTS = 3
KEY_ENV = {                                    # host -> (environment variable, query parameter)
    "eutils.ncbi.nlm.nih.gov": ("NCBI_API_KEY", "api_key"),
    "api.openalex.org": ("OPENALEX_API_KEY", "api_key"),
}


class HostGuard:
    def __init__(self, allowed_hosts, *, allow_http_loopback=True):
        self.allowed = {str(h).lower() for h in allowed_hosts or []}
        self.allow_http_loopback = allow_http_loopback

    def check(self, url: str) -> str:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if not host or host not in self.allowed:
            raise HostNotAllowed(f"{host or url!r} is not in the task's allowed hosts")
        if parsed.scheme != "https" and not (parsed.scheme == "http" and self.allow_http_loopback and host in LOOPBACK):
            raise HostNotAllowed(f"{host}: only https is allowed")
        return host


class SourceHttp:
    """A polite, guarded HTTP client for one task."""

    def __init__(self, allowed_hosts, *, min_interval_ms=1200, session=None, sleep=time.sleep, clock=time.monotonic,
                 timeout=(10, 45), max_bytes=MAX_BYTES, environ=None):
        self.guard = HostGuard(allowed_hosts)
        self.min_interval = max(0.0, float(min_interval_ms or 0) / 1000.0)
        self.session = session or requests.Session()
        self.sleep, self.clock = sleep, clock
        self.timeout, self.max_bytes = timeout, max_bytes
        self.environ = os.environ if environ is None else environ
        self.last_request: dict[str, float] = {}
        self.requests_made = 0

    # ---------------------------------------------------------------------------------------------- public
    def get_json(self, url):
        body = self._get(url, "application/json")
        try:
            return json.loads(body)
        except ValueError as exc:
            raise SourceError("the database answered something that is not JSON", code="source_bad_answer") from exc

    def get_text(self, url):
        return self._get(url, "application/xml, text/xml, */*")

    # ---------------------------------------------------------------------------------------------- internals
    def _with_key(self, url, host):
        spec = KEY_ENV.get(host)
        if not spec:
            return url
        key = (self.environ.get(spec[0]) or "").strip()
        if not key:
            return url
        parsed = urlparse(url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query.setdefault(spec[1], key)
        return urlunparse(parsed._replace(query=urlencode(query)))

    def _wait_turn(self, host):
        last = self.last_request.get(host)
        if last is not None and self.min_interval:
            remaining = self.min_interval - (self.clock() - last)
            if remaining > 0:
                self.sleep(remaining)
        self.last_request[host] = self.clock()

    def _get(self, url, accept):
        for _hop in range(MAX_REDIRECTS + 1):
            host = self.guard.check(url)
            self._wait_turn(host)
            self.requests_made += 1
            try:
                response = self.session.get(self._with_key(url, host), headers={"Accept": accept, "User-Agent": f"spacebio-satellite/{__version__}"},
                                            timeout=self.timeout, allow_redirects=False, stream=True)
            except (requests.ConnectionError, requests.Timeout) as exc:
                raise SourceError(f"could not reach {host}: {type(exc).__name__}", code="source_unavailable") from exc
            try:
                status = response.status_code
                if status in (301, 302, 303, 307, 308) and response.headers.get("Location"):
                    url = requests.compat.urljoin(url, response.headers["Location"])
                    continue
                if status == 429 or status >= 500:
                    retry = response.headers.get("Retry-After", "")
                    raise SourceError(f"{host} is busy (HTTP {status})", busy=True, status=status, retry_after=int(retry) if retry.isdigit() else None,
                                      code="rate_limited_by_source" if status == 429 else "source_unavailable")
                if status in (401, 403):
                    env = KEY_ENV.get(host, ("",))[0]
                    raise SourceError(f"{host} answered HTTP {status}: it wants an API key" + (f" (set {env} with your own free key)" if env else ""),
                                      status=status, code="source_needs_key")
                if status >= 400:
                    raise SourceError(f"{host} answered HTTP {status}{self._snippet(response)}", status=status, code="source_unavailable")
                return self._read(response, host)
            finally:
                response.close()
        raise SourceError(f"{host}: too many redirects", code="source_unavailable")

    @staticmethod
    def _snippet(response, limit=160):
        """The start of an error answer, one line, so a failure says WHY (the databases explain themselves in the body)."""
        try:
            raw = next(response.iter_content(chunk_size=limit * 2), b"")
        except Exception:                                               # an unreadable body is not worth a second error
            return ""
        text = " ".join(raw.decode("utf-8", errors="replace").split())[:limit]
        return f": {text}" if text else ""

    def _read(self, response, host):
        chunks, size = [], 0
        for chunk in response.iter_content(chunk_size=65536):
            size += len(chunk)
            if size > self.max_bytes:
                raise SourceError(f"{host}: the answer is larger than {self.max_bytes // 1_000_000} MB, refusing it", code="source_bad_answer")
            chunks.append(chunk)
        data = b"".join(chunks)
        content_type = response.headers.get("Content-Type", "").lower()
        encoding = content_type.split("charset=")[1].split(";")[0].strip() if "charset=" in content_type else "utf-8"   # not requests' ISO-8859-1 guess
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            return data.decode("utf-8", errors="replace")
