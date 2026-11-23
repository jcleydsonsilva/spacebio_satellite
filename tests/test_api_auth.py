"""The API client and the login flow, against the mock core."""
import pytest

from spacebio_satellite import API_MAJOR, auth, errors
from spacebio_satellite.api import ApiClient
from spacebio_satellite.output import Output


def client(core, token="", **kw):
    sleeps = kw.pop("sleeps", [])
    return ApiClient(core.url, token, sleep=sleeps.append, **kw), sleeps


# ---------------------------------------------------------------------------------------------------- requests
def test_meta_needs_no_token_and_identifies_the_client(core):
    api, _ = client(core)
    meta = api.meta()
    assert meta["api_version"] == "1.0" and api.api_version == "1.0"
    sent = core.requests_to("/api/satellite/v1")[-1]
    assert "Authorization" not in sent["headers"] and sent["headers"]["User-Agent"].startswith("spacebio-satellite/") and sent["headers"]["X-Satellite-Client"]


def test_authenticated_calls_send_the_bearer_token_and_remember_its_expiry(core, token):
    api, _ = client(core, token)
    api.me()
    sent = core.requests_to("/me")[-1]
    assert sent["headers"]["Authorization"] == f"Bearer {token}" and api.token_expires.endswith("Z")


def test_no_token_means_not_logged_in_without_touching_the_network(core):
    api, _ = client(core)
    with pytest.raises(errors.NotLoggedIn):
        api.me()
    assert core.log == []


def test_big_bodies_are_gzipped_small_ones_are_not(core, token):
    core.add_task("europepmc")
    api, _ = client(core, token)
    api.claim(capabilities=["harvest/1"])
    assert core.requests_to("tasks/claim")[-1]["gzip"] is False
    big = {"client_batch_id": "c1", "records": [{"title": "x" * 200, "doi": f"10.1234/{i}"} for i in range(50)]}
    api.submit_batch(1, big)
    assert core.requests_to("batches")[-1]["gzip"] is True and len(core.requests_to("batches")[-1]["body"]["records"]) == 50


def test_claim_sends_what_the_contract_lists(core, token):
    api, _ = client(core, token)
    api.claim(capabilities=["harvest/1"], focus=["radiation"], focus_only=True, skip_sources=["openalex"], max_tasks=1, dry_run=True)
    body = core.requests_to("tasks/claim")[-1]["body"]
    assert body == {"capabilities": ["harvest/1"], "focus": ["radiation"], "focus_only": True, "skip_sources": ["openalex"], "max_tasks": 1, "dry_run": True}


# ---------------------------------------------------------------------------------------------------- errors
@pytest.mark.parametrize("status,code,cls", [
    (401, "invalid_token", errors.InvalidToken), (403, "suspended", errors.Suspended), (403, "not_consented", errors.NotConsented),
    (403, "insufficient_scope", errors.InsufficientScope), (409, "task_not_yours_or_expired", errors.TaskExpired), (409, "duplicate_batch", errors.DuplicateBatch),
    (413, "too_large", errors.TooLarge), (422, "invalid_payload", errors.InvalidPayload), (426, "upgrade_required", errors.UpgradeRequired),
    (429, "rate_limited", errors.RateLimited), (503, "unavailable", errors.Unavailable),
])
def test_every_documented_error_becomes_its_own_exception(core, token, status, code, cls):
    core.fail.append({"match": "me", "status": status, "body": {"error": {"code": code, "message": "m", "retry_after": 12, "details": {"batch_id": "b1"}}}, "times": 99})
    api, _ = client(core, token, retries=0)
    with pytest.raises(cls) as caught:
        api.me()
    assert caught.value.code == code and caught.value.status == status and caught.value.retry_after == 12 and caught.value.details == {"batch_id": "b1"}


def test_the_retry_after_header_is_used_when_the_body_has_none(core, token):
    core.fail.append({"match": "me", "status": 429, "body": {"error": {"code": "rate_limited"}}, "headers": {"Retry-After": "9"}, "times": 99})
    with pytest.raises(errors.RateLimited) as caught:
        client(core, token, retries=0)[0].me()
    assert caught.value.retry_after == 9


def test_an_error_with_a_strange_body_still_gives_a_sensible_exception(core, token):
    core.fail.append({"match": "me", "status": 500, "body": {"nonsense": True}, "times": 99})
    with pytest.raises(errors.Unavailable):
        client(core, token, retries=0)[0].me()
    core.fail.clear()
    core.fail.append({"match": "me", "status": 401, "body": {"error": "invalid_token"}, "times": 99})            # the older flat shape
    with pytest.raises(errors.InvalidToken):
        client(core, token, retries=0)[0].me()


def test_a_redirect_is_an_error_and_the_token_is_not_forwarded(core, token):
    core.fail.append({"match": "me", "status": 302, "body": {}, "headers": {"Location": "https://evil.example.com/me"}, "times": 99})
    with pytest.raises(errors.ApiError) as caught:
        client(core, token, retries=0)[0].me()
    assert caught.value.code == "unexpected_redirect" and len(core.requests_to("/me")) == 1


def test_a_different_major_api_version_is_refused(core):
    core.api_version = "2.0"
    with pytest.raises(errors.UnsupportedApi):
        client(core)[0].meta()
    assert API_MAJOR == "1"


# ---------------------------------------------------------------------------------------------------- retries
def test_reads_are_retried_through_a_busy_server(core, token):
    core.fail.append({"match": "me", "status": 503, "times": 2})
    api, sleeps = client(core, token)
    assert api.me()["status"] == "active" and sleeps == [1, 2] and len(core.requests_to("/me")) == 3


def test_it_gives_up_after_the_retries(core, token):
    core.fail.append({"match": "me", "status": 503, "times": 99})
    api, _ = client(core, token, retries=2)
    with pytest.raises(errors.Unavailable):
        api.me()
    assert len(core.requests_to("/me")) == 3


def test_a_login_poll_is_not_retried_because_it_is_not_safe_to_repeat(core):
    core.fail.append({"match": "device/token", "status": 503, "times": 5})
    with pytest.raises(errors.Unavailable):
        client(core)[0].device_token("abc")
    assert len(core.requests_to("device/token")) == 1


def test_a_batch_upload_is_retried_because_the_batch_id_makes_it_safe(core, token):
    core.add_task("europepmc")
    api, _ = client(core, token)
    api.claim(capabilities=["harvest/1"])
    core.fail.append({"match": "batches", "status": 503, "times": 1})
    answer = api.submit_batch(1, {"client_batch_id": "same-id", "records": []})
    assert answer["status"] == "received" and len(core.requests_to("/batches")) == 2


def test_network_down_is_a_network_error(token):
    api = ApiClient("http://127.0.0.1:9", token, sleep=lambda s: None, retries=1)
    with pytest.raises(errors.NetworkError) as caught:
        api.me()
    assert "doctor" in caught.value.hint


# ---------------------------------------------------------------------------------------------------- login
def login(core, **kw):
    sleeps, opened = [], []
    out = Output(json_mode=False)
    api = ApiClient(core.url, "", sleep=lambda s: None)
    result = auth.login(api, out, label="Home laptop", sleep=sleeps.append, clock=kw.pop("clock", lambda: 0.0), opener=opened.append, **kw)
    return result, sleeps, opened


def test_the_device_flow_waits_for_approval(core):
    core.approve_after = 2
    result, sleeps, opened = login(core)
    assert result["access_token"].startswith("sbs_") and result["device_id"] and result["scopes"] == ["harvest:run"]
    assert sleeps == [1, 1, 1] and opened == [core.url + "/satellite/activate/?code=WDJB-MJHT"]
    assert core.requests_to("device/code")[0]["body"]["device_label"] == "Home laptop"


def test_slow_down_adds_five_seconds(core):
    core.approve_after, core.slow_down_once = 0, True
    _, sleeps, _ = login(core)
    assert sleeps == [1, 6]


def test_the_browser_is_optional(core, capsys):
    core.approve_after = 0
    _, _, opened = login(core, open_browser=False)
    assert opened == [] and "Open  " in capsys.readouterr().out


def test_a_missing_browser_does_not_break_login(core):
    core.approve_after = 0
    api = ApiClient(core.url, "", sleep=lambda s: None)

    def broken(url):
        raise RuntimeError("no browser")

    assert auth.login(api, Output(), label="x", sleep=lambda s: None, clock=lambda: 0.0, opener=broken)["access_token"]


def test_refusal_is_reported(core):
    core.deny = True
    with pytest.raises(errors.LoginDenied):
        login(core)


def test_the_code_expiring_on_the_server_or_by_the_clock_is_reported(core):
    core.fail.append({"match": "device/token", "status": 400, "body": {"error": {"code": "expired_token"}}, "times": 1})
    with pytest.raises(errors.LoginExpired):
        login(core)
    core.approve_after = 99
    ticks = iter([0, 0, 10_000, 10_000, 10_000])
    with pytest.raises(errors.LoginExpired):
        login(core, clock=lambda: next(ticks))


def test_an_old_client_is_told_to_update_before_any_code_is_requested(core):
    core.min_client_version = "9.0.0"
    with pytest.raises(errors.UpgradeRequired) as caught:
        login(core)
    assert "pipx upgrade" in caught.value.hint and core.requests_to("device/code") == []


def test_pending_is_not_mistaken_for_other_errors(core):
    core.fail.append({"match": "device/token", "status": 500, "body": {"error": {"code": "unavailable"}}, "times": 1})
    with pytest.raises(errors.Unavailable):
        login(core)
