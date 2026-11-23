"""The client of the SpaceBio Satellite API v1 (see docs/CONTRACT.md). One method per endpoint, errors as exceptions.

Safety rules baked in: the token only travels over https (or to localhost), redirects are never followed (so a token cannot be
forwarded to another host), big bodies are gzipped, and only calls that are safe to repeat are retried.
"""
from __future__ import annotations

import gzip
import json
import platform
import time

import requests

from . import API_MAJOR, __version__
from .errors import ApiError, NetworkError, NotLoggedIn, UnsupportedApi, error_from_response

PREFIX = "/api/satellite/v1/"
GZIP_OVER = 1024
RETRY_STATUS = (502, 503, 504)


def user_agent() -> str:
    return f"spacebio-satellite/{__version__} ({platform.system() or 'unknown'})"


class ApiClient:
    def __init__(self, server, token="", *, session=None, sleep=time.sleep, retries=3, timeout=(10, 60)):
        self.server = server.rstrip("/")
        self.token = token
        self.session = session or requests.Session()
        self.sleep, self.retries, self.timeout = sleep, retries, timeout
        self.api_version = ""
        self.token_expires = ""
        self.headers = {"User-Agent": user_agent(), "X-Satellite-Client": __version__, "Accept": "application/json"}

    # ---------------------------------------------------------------------------------------------- transport
    def _request(self, method, path, *, body=None, params=None, auth=True, idempotent=False, timeout=None):
        headers = dict(self.headers)
        if auth:
            if not self.token:
                raise NotLoggedIn("You are not logged in.")
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            if len(data) > GZIP_OVER:
                data = gzip.compress(data)
                headers["Content-Encoding"] = "gzip"
            headers["Content-Type"] = "application/json"
        url = self.server + PREFIX + path.lstrip("/")
        repeatable = idempotent or method == "GET"
        attempt = 0
        while True:
            try:
                response = self.session.request(method, url, data=data, params=params, headers=headers, timeout=timeout or self.timeout, allow_redirects=False)
            except (requests.ConnectionError, requests.Timeout) as exc:
                if repeatable and attempt < self.retries:
                    self.sleep(min(2 ** attempt, 30))
                    attempt += 1
                    continue
                raise NetworkError(f"Could not reach {self.server}: {type(exc).__name__}", hint="Check your connection, or run: spacebio-satellite doctor") from exc
            if response.status_code in RETRY_STATUS and repeatable and attempt < self.retries:
                retry = response.headers.get("Retry-After", "")
                self.sleep(min(int(retry) if retry.isdigit() else 2 ** attempt, 60))
                attempt += 1
                continue
            break
        return self._finish(response)

    def _finish(self, response):
        status = response.status_code
        self.api_version = response.headers.get("X-Satellite-API", self.api_version)
        self.token_expires = response.headers.get("X-Satellite-Token-Expires", self.token_expires)
        if 300 <= status < 400:
            raise ApiError("unexpected_redirect", f"The server redirected to {response.headers.get('Location', '?')}. Check the server address.", status=status)
        try:
            body = response.json() if response.content else {}
        except ValueError:
            body = None
        if status >= 400:
            raise error_from_response(status, body, response.headers)
        if not isinstance(body, dict):
            raise ApiError("bad_response", "The server did not answer with the documented JSON.", status=status)
        if self.api_version and self.api_version.split(".")[0] != API_MAJOR:
            raise UnsupportedApi("unsupported_api", f"The server speaks API {self.api_version}, this program speaks {API_MAJOR}.x.", status=status)
        return body

    # ---------------------------------------------------------------------------------------------- no token needed
    def meta(self):
        return self._request("GET", "", auth=False)

    def device_code(self, label, client_version=__version__):
        return self._request("POST", "device/code", body={"client_name": "spacebio-satellite", "client_version": client_version, "device_label": label}, auth=False)

    def device_token(self, device_code):
        return self._request("POST", "device/token", body={"device_code": device_code}, auth=False)

    # ---------------------------------------------------------------------------------------------- with the token
    def me(self):
        return self._request("GET", "me")

    def set_focus(self, focus):
        return self._request("PATCH", "me", body={"focus": list(focus)}, idempotent=True)

    def claim(self, *, capabilities, focus=None, focus_only=False, skip_sources=(), max_tasks=1, dry_run=False):
        body = {"capabilities": list(capabilities), "focus_only": bool(focus_only), "skip_sources": list(skip_sources), "max_tasks": int(max_tasks), "dry_run": bool(dry_run)}
        if focus is not None:
            body["focus"] = list(focus)
        return self._request("POST", "tasks/claim", body=body, idempotent=True)

    def heartbeat(self, task_id):
        return self._request("POST", f"tasks/{int(task_id)}/heartbeat", body={}, idempotent=True)

    def release(self, task_id, reason, detail=""):
        return self._request("POST", f"tasks/{int(task_id)}/release", body={"reason": reason, "detail": str(detail)[:200]}, idempotent=True)

    def submit_batch(self, task_id, batch):
        """Safe to retry: the core answers a repeated ``client_batch_id`` with ``duplicate_batch`` instead of storing it twice."""
        return self._request("POST", f"tasks/{int(task_id)}/batches", body=batch, idempotent=True)

    def get_batch(self, batch_id, wait=0):
        wait = max(0, min(int(wait), 30))
        return self._request("GET", f"batches/{batch_id}", params={"wait": wait} if wait else None, timeout=(10, wait + 20))

    def revoke(self):
        return self._request("POST", "device/revoke", body={}, idempotent=True)
