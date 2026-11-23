"""A pretend SpaceBio core (and pretend public databases) that follows docs/CONTRACT.md, for tests and for trying the CLI offline.

    python devtools/mock_core.py --port 8765 --tasks 6
    export SPACEBIO_SATELLITE_URL=http://127.0.0.1:8765
    spacebio-satellite login --no-browser        # approves itself after one poll
    spacebio-satellite run --once

The databases it serves are the REAL answers recorded in tests/fixtures (Europe PMC, PubMed, Crossref, OpenAlex), split in pages.
It is a test double, not the real server: it checks the parts of the contract the client depends on and nothing more.
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import secrets
import threading
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

API = "/api/satellite/v1/"
FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def _iso(moment):
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_verdict(batch):
    n = len(batch["payload"].get("records") or [])
    return {"status": "accepted", "reason": "", "counts": {"received": n, "invalid": 0, "off_topic": 0, "known": 0, "new": n, "completed": 0,
                                                              "verified_ok": min(n, 10), "verified_bad": 0, "verified_unknown": 0},
            "points": 10 * n + 1, "suspended": False}


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):          # a client that hangs up early is normal (it refused the answer): no traceback noise
        pass


class MockCore:
    def __init__(self, fixtures=FIXTURES):
        self.fixtures = Path(fixtures)
        self.server = None
        self.thread = None
        self.reset()

    def reset(self):
        self.api_version = "1.0"
        self.min_client_version = "0.1.0"
        self.latest_client_version = "0.1.0"
        self.account_status = "active"                 # active | paused | suspended | consent_needed
        self.tier, self.trust, self.points_total, self.points_today, self.daily_points = "newcomer", 4, 120, 0, 400
        self.focus = []
        self.claim_min_interval_s = 0
        self.max_records_per_batch = 1000
        self.approve_after = 1                         # how many polls answer "authorization_pending" first
        self.slow_down_once = False
        self.deny = False
        self.tokens = {}                               # token -> device id
        self.device_codes = {}
        self.tasks = []                                # queue of task dicts
        self.claimed = {}
        self.released = []
        self.heartbeats = []
        self.batches = {}
        self.client_ids = {}
        self.verdict_fn = default_verdict
        self.polls_before_final = 0
        self.expired_tasks = set()                     # task ids that answer 409 when a batch arrives
        self.log = []
        self.source_log = []
        self.source_status = {}                        # source name -> HTTP status to answer (429, 503...)
        self.fail = []                                 # [{"match": "tasks/claim", "status": 503, "times": 1}]
        self.claim_reason = "no_tasks"
        self.paging_needs_key = set()                  # source names that answer 401 beyond the first page, like OpenAlex does without a key
        self.ignore_capabilities = False               # True: hand out kinds the client did not list (to test its refusal)
        self.lease_minutes = 15

    # ================================================================================================ helpers for tests
    @property
    def url(self):
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def make_task(self, source="europepmc", task_id=1, pages=3, page_size=2, keyword="microgravity", lease_minutes=None, **extra):
        base = self.url
        urls = {
            "europepmc": f"{base}/europepmc/webservices/rest/search?query={keyword}&format=json&resultType=core&pageSize={page_size}&cursorMark=*",
            "pubmed": f"{base}/entrez/eutils/esearch.fcgi?db=pubmed&term={keyword}&retmax={page_size}&retstart=0&retmode=json&tool=SpaceBio",
            "crossref": f"{base}/crossref/works?query.bibliographic={keyword}&rows={page_size}&cursor=*",
            "openalex": f"{base}/openalex/works?search={keyword}&per-page={page_size}&cursor=*",
        }
        task = {"id": task_id, "kind": "harvest/1", "pipeline": "Papers end to end", "source": source, "keyword": keyword,
                "window": {"start": "2023-01-01", "end": "2023-12-31"}, "request": {"url": urls.get(source, base + "/unknown"), "pages": pages, "page_size": page_size},
                "allowed_hosts": ["127.0.0.1"], "min_interval_ms": 0, "contact_email": "project@example.org", "record_schema": "paper/1"}
        task.update(extra)
        self._lease_fields(task, lease_minutes)
        return task

    def _lease_fields(self, task, minutes=None):
        now = datetime.now(timezone.utc)
        task["lease_until"] = _iso(now + timedelta(minutes=self.lease_minutes if minutes is None else minutes))
        task["max_lease_until"] = _iso(now + timedelta(minutes=60))

    def add_task(self, source="europepmc", **kw):
        task = self.make_task(source, task_id=kw.pop("task_id", len(self.tasks) + len(self.claimed) + 1), **kw)
        self.tasks.append(task)
        return task

    def requests_to(self, fragment):
        return [r for r in self.log if fragment in r["path"]]

    # ================================================================================================ lifecycle
    def start(self, host="127.0.0.1", port=0):
        core = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                core.handle(self, "GET")

            def do_POST(self):
                core.handle(self, "POST")

            def do_PATCH(self):
                core.handle(self, "PATCH")

        self.server = QuietServer((host, port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)

    # ================================================================================================ plumbing
    def send(self, h, status, body, headers=None, raw=None, content_type="application/json"):
        data = raw if raw is not None else json.dumps(body).encode("utf-8")
        h.send_response(status)
        h.send_header("Content-Type", content_type)
        h.send_header("Content-Length", str(len(data)))
        if str(h.path).startswith(API):
            h.send_header("X-Satellite-API", self.api_version)
        for k, v in (headers or {}).items():
            h.send_header(k, str(v))
        h.end_headers()
        h.wfile.write(data)

    def error(self, h, status, code, message="", **extra):
        headers = {"Retry-After": extra["retry_after"]} if "retry_after" in extra else None
        self.send(h, status, {"error": {"code": code, "message": message or code, **extra}}, headers)

    def handle(self, h, method):
        parsed = urlparse(h.path)
        length = int(h.headers.get("Content-Length") or 0)
        raw = h.rfile.read(length) if length else b""
        if h.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = None
        query = {k: v[-1] for k, v in parse_qs(parsed.query, keep_blank_values=True).items()}
        entry = {"method": method, "path": parsed.path, "query": query, "headers": {k: v for k, v in h.headers.items()}, "body": body, "gzip": h.headers.get("Content-Encoding") == "gzip"}
        self.log.append(entry)
        if parsed.path.startswith(API):
            return self.api(h, method, parsed.path[len(API):].strip("/"), query, body, entry)
        return self.source(h, parsed.path, query)

    # ================================================================================================ the API
    def token_of(self, entry):
        auth = entry["headers"].get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        return token if token in self.tokens else ""

    def api(self, h, method, path, query, body, entry):
        for rule in self.fail:
            if rule["match"] in path and rule.get("times", 1) > 0:
                rule["times"] = rule.get("times", 1) - 1
                return self.send(h, rule.get("status", 503), rule.get("body", {"error": {"code": "unavailable", "message": "try later"}}), rule.get("headers"))
        if path == "" and method == "GET":
            return self.send(h, 200, {"api_version": self.api_version, "min_client_version": self.min_client_version, "latest_client_version": self.latest_client_version,
                                      "server_time": _iso(datetime.now(timezone.utc)), "consent_version": 1, "task_kinds": ["harvest/1"], "record_schemas": ["paper/1"],
                                      "embedding": {"model": "test-model", "revision": "r1", "dim": 768}})
        if path == "device/code" and method == "POST":
            code = secrets.token_hex(16)
            self.device_codes[code] = {"polls": 0, "label": (body or {}).get("device_label", "")}
            return self.send(h, 200, {"device_code": code, "user_code": "WDJB-MJHT", "verification_uri": self.url + "/satellite/activate/",
                                      "verification_uri_complete": self.url + "/satellite/activate/?code=WDJB-MJHT", "expires_in": 600, "interval": 1})
        if path == "device/token" and method == "POST":
            info = self.device_codes.get((body or {}).get("device_code"))
            if info is None:
                return self.error(h, 400, "expired_token")
            info["polls"] += 1
            if self.deny:
                return self.error(h, 400, "access_denied")
            if self.slow_down_once:
                self.slow_down_once = False
                return self.error(h, 400, "slow_down")
            if info["polls"] <= self.approve_after:
                return self.error(h, 400, "authorization_pending")
            token = "sbs_" + secrets.token_urlsafe(32)
            device_id = str(uuid.uuid4())
            self.tokens[token] = device_id
            return self.send(h, 200, {"access_token": token, "token_type": "Bearer", "expires_at": _iso(datetime.now(timezone.utc) + timedelta(days=90)),
                                      "scopes": ["harvest:run"], "device_id": device_id})
        token = self.token_of(entry)
        if not token:
            return self.error(h, 401, "invalid_token", "Missing, unknown, expired or revoked token.")
        extra = {"X-Satellite-Token-Expires": _iso(datetime.now(timezone.utc) + timedelta(days=90))}
        if path == "me" and method == "GET":
            return self.send(h, 200, self.me_body(token), extra)
        if path == "me" and method == "PATCH":
            self.focus = [str(x) for x in (body or {}).get("focus", [])]
            return self.send(h, 200, self.me_body(token), extra)
        if path == "device/revoke" and method == "POST":
            self.tokens.pop(token, None)
            return self.send(h, 200, {"revoked": True})
        if self.account_status == "suspended":
            return self.error(h, 403, "suspended", "Account suspended.")
        if self.account_status == "consent_needed":
            return self.error(h, 403, "not_consented", "Consent needed.")
        if path == "tasks/claim" and method == "POST":
            return self.claim(h, body or {}, extra)
        m = re.match(r"^tasks/(\d+)/(heartbeat|release|batches)$", path)
        if m and method == "POST":
            return self.task_action(h, token, int(m.group(1)), m.group(2), body or {}, extra)
        m = re.match(r"^batches/([\w-]+)$", path)
        if m and method == "GET":
            return self.batch_status(h, m.group(1), extra)
        return self.error(h, 404, "not_found")

    def me_body(self, token):
        return {"status": self.account_status, "tier": self.tier, "trust": self.trust, "points_total": self.points_total, "points_today": self.points_today,
                "daily_points": self.daily_points, "points_left": self.daily_points - self.points_today, "tasks_last_hour": 0, "max_tasks_per_hour": 30, "focus": self.focus,
                "device": {"id": self.tokens.get(token), "label": "Test", "created_at": _iso(datetime.now(timezone.utc)), "expires_at": _iso(datetime.now(timezone.utc) + timedelta(days=90))},
                "limits": {"claim_min_interval_s": self.claim_min_interval_s, "max_tasks_per_claim": 1, "max_body_bytes": 2_000_000, "max_records_per_batch": self.max_records_per_batch}}

    def claim(self, h, body, extra):
        limits = {"max_records_per_batch": self.max_records_per_batch}
        if self.account_status == "paused":
            return self.send(h, 200, {"tasks": [], "reason": "paused", "retry_after": 300}, extra)
        skip = set(body.get("skip_sources") or [])
        caps = set(body.get("capabilities") or [])
        for task in self.tasks:
            if task["source"] in skip or (not self.ignore_capabilities and task["kind"] not in caps):
                continue
            if body.get("dry_run"):
                peek = dict(task)
                peek["lease_until"] = None
                return self.send(h, 200, {"tasks": [peek], "limits": limits}, extra)
            self.tasks.remove(task)
            self._lease_fields(task)
            self.claimed[task["id"]] = task
            return self.send(h, 200, {"tasks": [task], "limits": limits}, extra)
        return self.send(h, 200, {"tasks": [], "reason": self.claim_reason, "retry_after": 900}, extra)

    def task_action(self, h, token, task_id, action, body, extra):
        task = self.claimed.get(task_id)
        if action == "batches":
            cid = body.get("client_batch_id")
            if (token, cid) in self.client_ids:
                original = self.batches[self.client_ids[(token, cid)]]
                return self.error(h, 409, "duplicate_batch", "Already received.", details={"batch_id": original["id"], "status": original["status"]})
        if task is None or task_id in self.expired_tasks:
            return self.error(h, 409, "task_not_yours_or_expired", "The lease expired.")
        if action == "heartbeat":
            self.heartbeats.append(task_id)
            self._lease_fields(task)
            return self.send(h, 200, {"lease_until": task["lease_until"], "max_lease_until": task["max_lease_until"]}, extra)
        if action == "release":
            self.released.append({"task_id": task_id, **body})
            del self.claimed[task_id]
            self.tasks.append(task)
            return self.send(h, 200, {"status": "released"}, extra)
        records = body.get("records") or []
        if len(records) > self.max_records_per_batch:
            return self.error(h, 413, "too_large", "Too many records.")
        batch = {"id": str(uuid.uuid4()), "task_id": task_id, "payload": body, "status": "received", "polls": 0}
        self.batches[batch["id"]] = batch
        self.client_ids[(token, body.get("client_batch_id"))] = batch["id"]
        del self.claimed[task_id]
        return self.send(h, 202, {"batch_id": batch["id"], "status": "received"}, extra)

    def batch_status(self, h, batch_id, extra):
        batch = self.batches.get(batch_id)
        if batch is None:
            return self.error(h, 404, "not_found")
        batch["polls"] += 1
        if batch["polls"] <= self.polls_before_final:
            return self.send(h, 200, {"batch_id": batch_id, "task_id": batch["task_id"], "status": "processing"}, extra)
        verdict = self.verdict_fn(batch)
        batch["status"] = verdict["status"]
        return self.send(h, 200, {"batch_id": batch_id, "task_id": batch["task_id"], "limits": {"points_left": 0}, **verdict}, extra)

    # ================================================================================================ the pretend databases
    def load(self, name):
        text = (self.fixtures / name).read_text(encoding="utf-8")
        return json.loads(text) if name.endswith(".json") else text

    def source(self, h, path, q):
        self.source_log.append({"path": path, "query": q, "headers": {k: v for k, v in h.headers.items()}})
        name = "europepmc" if path.startswith("/europepmc") else "pubmed" if path.startswith("/entrez") else "crossref" if path.startswith("/crossref") else "openalex" if path.startswith("/openalex") else ""
        if name in self.source_status:
            status = self.source_status[name]
            return self.send(h, status, {"busy": True}, {"Retry-After": "7"})
        if name in self.paging_needs_key and q.get("cursor", "*") != "*":
            return self.send(h, 401, {"error": "API key required for cursor paging", "message": "Paging past the first page with cursor= needs a free API key."})
        if name == "europepmc":
            data = self.load("europepmc.json")
            results = data["resultList"]["result"]
            first = q.get("cursorMark", "*") == "*"
            data["resultList"]["result"] = results[:2] if first else results[2:]
            data["nextCursorMark"] = "c2"
            return self.send(h, 200, data)
        if name == "pubmed" and path.endswith("esearch.fcgi"):
            data = self.load("pubmed_search.json")
            ids = data["esearchresult"]["idlist"]
            start, size = int(q.get("retstart", 0)), int(q.get("retmax", 20))
            data["esearchresult"].update({"idlist": ids[start:start + size], "retstart": str(start), "retmax": str(size), "count": str(len(ids))})
            return self.send(h, 200, data)
        if name == "pubmed" and path.endswith("efetch.fcgi"):
            wanted = set(q.get("id", "").split(","))
            xml = self.load("pubmed_fetch.xml")
            root = ET.fromstring(re.sub(r"<!DOCTYPE[^>]*>", "", xml))
            for art in list(root):
                pmid = art.findtext("MedlineCitation/PMID")
                if pmid not in wanted:
                    root.remove(art)
            return self.send(h, 200, None, raw=ET.tostring(root, encoding="utf-8", xml_declaration=True), content_type="text/xml; charset=utf-8")
        if name == "crossref":
            data = self.load("crossref.json")
            items = data["message"]["items"]
            cursor = q.get("cursor", "*")
            data["message"]["items"] = items[:2] if cursor == "*" else items[2:] if cursor == "c2" else []
            data["message"]["next-cursor"] = "c2" if cursor == "*" else "c3"
            return self.send(h, 200, data)
        if name == "openalex":
            data = self.load("openalex.json")
            results = data["results"]
            cursor = q.get("cursor", "*")
            data["results"] = results[:2] if cursor == "*" else results[2:]
            data["meta"]["next_cursor"] = "c2" if cursor == "*" else None
            return self.send(h, 200, data)
        return self.error(h, 404, "not_found")


def main(argv=None):
    parser = argparse.ArgumentParser(description="A pretend SpaceBio core for trying spacebio-satellite offline.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--tasks", type=int, default=6, help="how many tasks to offer (cycling over the four databases)")
    parser.add_argument("--approve-after", type=int, default=1)
    args = parser.parse_args(argv)
    core = MockCore()
    core.approve_after = args.approve_after
    core.start(port=args.port)
    for i in range(args.tasks):
        core.add_task(["europepmc", "pubmed", "crossref", "openalex"][i % 4], task_id=i + 1)
    print(f"Mock SpaceBio core on {core.url}  (Ctrl-C to stop)\n\n  export SPACEBIO_SATELLITE_URL={core.url}\n  export SPACEBIO_SATELLITE_HOME=/tmp/satellite-demo   # keep the demo apart from your real settings\n"
          f"  spacebio-satellite login --no-browser --allow-plaintext-token\n  spacebio-satellite run --once\n")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        core.stop()


if __name__ == "__main__":
    main()
