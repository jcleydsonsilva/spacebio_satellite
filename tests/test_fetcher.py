"""The guarded HTTP client for the public databases: hosts, pacing, redirects, size, busy answers, own API keys."""
import pytest
import requests

from spacebio_satellite.errors import HostNotAllowed, SourceError
from spacebio_satellite.fetcher import HostGuard, SourceHttp


class FakeResponse:
    def __init__(self, status=200, body=b"{}", headers=None):
        self.status_code, self.body, self.headers = status, body, headers or {}
        self.closed = False

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self.body), chunk_size):
            yield self.body[i:i + chunk_size]

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item


class Clock:
    def __init__(self):
        self.t, self.sleeps = 0.0, []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def http(session, *, hosts=("api.example.org",), interval=0, clock=None, **kw):
    clock = clock or Clock()
    return SourceHttp(hosts, min_interval_ms=interval, session=session, sleep=clock.sleep, clock=clock, environ=kw.pop("environ", {}), **kw)


def test_only_listed_hosts_over_https():
    guard = HostGuard(["api.example.org"])
    assert guard.check("https://api.example.org/x") == "api.example.org"
    for bad in ("https://evil.example.com/x", "http://api.example.org/x", "ftp://api.example.org/x", "not a url", "https://api.example.org.evil.com/x"):
        with pytest.raises(HostNotAllowed):
            guard.check(bad)


def test_plain_http_is_only_allowed_for_this_computer():
    guard = HostGuard(["127.0.0.1", "api.example.org"])
    assert guard.check("http://127.0.0.1:8000/x") == "127.0.0.1"
    with pytest.raises(HostNotAllowed):
        guard.check("http://api.example.org/x")


def test_a_forbidden_host_is_never_contacted():
    session = FakeSession(FakeResponse())
    with pytest.raises(HostNotAllowed):
        http(session).get_json("https://evil.example.com/x")
    assert session.calls == []


def test_requests_to_one_host_are_paced():
    clock, session = Clock(), FakeSession(FakeResponse())
    client = http(session, interval=1200, clock=clock)
    for _ in range(3):
        client.get_json("https://api.example.org/x")
    assert clock.sleeps and all(abs(s - 1.2) < 1e-9 for s in clock.sleeps) and len(clock.sleeps) == 2


def test_no_wait_when_the_interval_already_passed():
    clock, session = Clock(), FakeSession(FakeResponse())
    client = http(session, interval=1000, clock=clock)
    client.get_json("https://api.example.org/x")
    clock.t += 5
    client.get_json("https://api.example.org/x")
    assert clock.sleeps == []


def test_busy_answers_give_the_task_back():
    err = pytest.raises(SourceError, http(FakeSession(FakeResponse(429, headers={"Retry-After": "30"}))).get_json, "https://api.example.org/x")
    assert err.value.busy and err.value.code == "rate_limited_by_source" and err.value.retry_after == 30
    err = pytest.raises(SourceError, http(FakeSession(FakeResponse(503))).get_json, "https://api.example.org/x")
    assert err.value.busy and err.value.code == "source_unavailable"


def test_other_errors_are_not_called_busy():
    err = pytest.raises(SourceError, http(FakeSession(FakeResponse(404))).get_json, "https://api.example.org/x")
    assert not err.value.busy and err.value.status == 404


def test_connection_problems_become_source_errors():
    err = pytest.raises(SourceError, http(FakeSession(requests.ConnectionError("down"))).get_json, "https://api.example.org/x")
    assert "could not reach" in str(err.value)


def test_redirects_are_followed_only_inside_the_allowed_hosts():
    ok = FakeSession(FakeResponse(302, headers={"Location": "/moved"}), FakeResponse(200, b'{"a": 1}'))
    assert http(ok).get_json("https://api.example.org/x") == {"a": 1}
    assert ok.calls[1][0] == "https://api.example.org/moved" and all(c[1]["allow_redirects"] is False for c in ok.calls)
    evil = FakeSession(FakeResponse(302, headers={"Location": "https://evil.example.com/steal"}))
    with pytest.raises(HostNotAllowed):
        http(evil).get_json("https://api.example.org/x")
    assert len(evil.calls) == 1


def test_a_redirect_loop_is_cut():
    loop = FakeSession(FakeResponse(302, headers={"Location": "/again"}))
    with pytest.raises(SourceError, match="too many redirects"):
        http(loop).get_json("https://api.example.org/x")


def test_oversized_answers_are_refused():
    with pytest.raises(SourceError, match="larger than"):
        http(FakeSession(FakeResponse(200, b"x" * 3_000_000)), max_bytes=1_000_000).get_json("https://api.example.org/x")


def test_json_that_is_not_json_is_a_bad_answer():
    err = pytest.raises(SourceError, http(FakeSession(FakeResponse(200, b"<html>oops</html>"))).get_json, "https://api.example.org/x")
    assert err.value.code == "source_bad_answer"


def test_xml_is_decoded_as_utf8_unless_told_otherwise():
    body = "<a>Síndrome — microgravidade</a>".encode("utf-8")
    assert http(FakeSession(FakeResponse(200, body, {"Content-Type": "text/xml"}))).get_text("https://api.example.org/x") == "<a>Síndrome — microgravidade</a>"
    latin = "<a>é</a>".encode("latin-1")
    assert http(FakeSession(FakeResponse(200, latin, {"Content-Type": "text/xml; charset=ISO-8859-1"}))).get_text("https://api.example.org/x") == "<a>é</a>"


def test_your_api_key_goes_only_to_its_own_host_and_never_overrides_the_url():
    env = {"OPENALEX_API_KEY": "mykey", "NCBI_API_KEY": "ncbikey"}
    session = FakeSession(FakeResponse())
    client = http(session, hosts=("api.openalex.org", "api.crossref.org", "eutils.ncbi.nlm.nih.gov"), environ=env)
    client.get_json("https://api.openalex.org/works?search=x")
    client.get_json("https://api.crossref.org/works?query=x")
    client.get_json("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed")
    client.get_json("https://api.openalex.org/works?api_key=fromtask")
    urls = [c[0] for c in session.calls]
    assert "api_key=mykey" in urls[0] and "api_key" not in urls[1] and "api_key=ncbikey" in urls[2] and urls[3].count("api_key") == 1 and "fromtask" in urls[3]


def test_every_request_identifies_the_program_and_asks_for_the_right_format():
    session = FakeSession(FakeResponse())
    http(session).get_json("https://api.example.org/x")
    headers = session.calls[0][1]["headers"]
    assert headers["User-Agent"].startswith("spacebio-satellite/") and headers["Accept"] == "application/json"


def test_responses_are_always_closed():
    response = FakeResponse(200, b"{}")
    http(FakeSession(response)).get_json("https://api.example.org/x")
    assert response.closed


def test_a_401_means_the_database_wants_a_key_and_says_which_variable_to_set():
    err = pytest.raises(SourceError, http(FakeSession(FakeResponse(401, b'{"error": "API key required"}')), hosts=("api.openalex.org",)).get_json, "https://api.openalex.org/works")
    assert err.value.code == "source_needs_key" and not err.value.busy and "OPENALEX_API_KEY" in str(err.value)
    err = pytest.raises(SourceError, http(FakeSession(FakeResponse(403))).get_json, "https://api.example.org/x")
    assert err.value.code == "source_needs_key" and "set " not in str(err.value)           # no variable to suggest for an unknown host


def test_an_error_answer_says_why_in_one_short_line():
    body = b'{"error": "API rate limit exceeded",\n  "count": "4"}' + b" " * 400
    err = pytest.raises(SourceError, http(FakeSession(FakeResponse(400, body))).get_json, "https://api.example.org/x")
    assert 'HTTP 400: {"error": "API rate limit exceeded", "count": "4"}' in str(err.value) and "\n" not in str(err.value) and len(str(err.value)) < 260
    quiet = pytest.raises(SourceError, http(FakeSession(FakeResponse(404, b""))).get_json, "https://api.example.org/x")
    assert str(quiet.value).endswith("HTTP 404")
