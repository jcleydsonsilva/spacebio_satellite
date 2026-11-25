"""The work loop against the mock core: the happy path and everything that can go wrong on the way."""
import json
import re

import pytest

from spacebio_satellite import errors
from spacebio_satellite.api import ApiClient
from spacebio_satellite.outbox import Outbox
from spacebio_satellite.output import Output
from spacebio_satellite.runner import RunOptions, Runner


class Rig:
    def __init__(self, core, token, tmp_path, fake_time, **kw):
        self.core, self.fake = core, fake_time
        self.api = ApiClient(core.url, token, sleep=fake_time.sleep)
        self.box = Outbox(tmp_path / "o.sqlite3")
        self.runner = Runner(self.api, self.box, core.url, Output(json_mode=kw.pop("json_mode", False)), sleep=fake_time.sleep, clock=fake_time.clock, **kw)

    def run(self, **options):
        return self.runner.run(RunOptions(**options))

    def posts(self, suffix):
        return [r for r in self.core.log if r["method"] == "POST" and r["path"].endswith(suffix)]


@pytest.fixture
def rig(core, token, tmp_path, fake_time):
    return Rig(core, token, tmp_path, fake_time)


def verdict(status="accepted", reason="", suspended=False, new=0):
    return lambda batch: {"status": status, "reason": reason, "counts": {"new": new}, "points": 0, "suspended": suspended}


# ---------------------------------------------------------------------------------------------------- the happy path
def test_one_task_from_claim_to_verdict(rig, core):
    core.add_task("europepmc")
    stats = rig.run(max_tasks=1)
    assert (stats.tasks, stats.batches, stats.new, stats.points, stats.stopped_reason) == (1, 1, 3, 31, "max_tasks")
    body = rig.posts("/batches")[0]["body"]
    assert len(body["records"]) == 3 and body["pages_fetched"] == 2 and body["error"] is None
    assert re.fullmatch(r"[0-9a-f-]{36}", body["client_batch_id"]) and body["started_at"].endswith("Z") and body["finished_at"].endswith("Z")
    assert rig.box.pending(core.url) == []
    assert rig.box.history(core.url)[0]["status"] == "accepted" and rig.box.history(core.url)[0]["new"] == 3


def test_what_is_sent_is_clean_paper_1(rig, core):
    core.add_task("openalex")
    rig.run(max_tasks=1)
    for record in rig.posts("/batches")[0]["body"]["records"]:
        assert set(record) == {"title", "doi", "pmid", "abstract", "authors", "journal", "year", "language", "urls"}
        assert not record["doi"].startswith("http") and "<" not in record["title"]


def test_all_four_databases_work_end_to_end(rig, core):
    for i, source in enumerate(["europepmc", "pubmed", "crossref", "openalex"], 1):
        core.add_task(source, task_id=i)
    stats = rig.run(max_tasks=4)
    assert stats.batches == 4 and stats.new == 12 and stats.stopped_reason == "max_tasks"


def test_the_claim_announces_only_what_this_client_can_do(rig, core):
    rig.run(once=True)
    assert core.requests_to("tasks/claim")[0]["body"]["capabilities"] == ["harvest/1"]


# ---------------------------------------------------------------------------------------------------- the core's verdicts
def test_a_rejected_batch_is_explained_and_removed(rig, core, capsys):
    core.add_task("europepmc")
    core.verdict_fn = verdict("rejected", "verification_failed")
    stats = rig.run(max_tasks=1)
    assert stats.rejected == 1 and stats.batches == 0 and rig.box.pending(core.url) == []
    assert "strike" in capsys.readouterr().err and rig.box.history(core.url)[0]["status"] == "rejected"


def test_retry_means_the_task_went_back_to_the_pool(rig, core):
    core.add_task("europepmc")
    core.verdict_fn = verdict("retry", "verification_unavailable")
    stats = rig.run(max_tasks=1)
    assert stats.retried == 1 and rig.box.pending(core.url) == []


def test_a_suspension_stops_everything_and_still_clears_the_outbox(rig, core):
    core.add_task("europepmc")
    core.verdict_fn = verdict("rejected", "verification_failed", suspended=True)
    with pytest.raises(errors.Suspended):
        rig.run(max_tasks=1)
    assert rig.box.pending(core.url) == []
    assert rig.runner.stats.rejected == 1


def test_the_verdict_is_polled_until_it_is_final(rig, core):
    core.add_task("europepmc")
    core.polls_before_final = 2
    stats = rig.run(max_tasks=1)
    assert stats.batches == 1 and len([r for r in core.log if r["method"] == "GET" and "/batches/" in r["path"]]) == 3
    assert all(r["query"].get("wait") == "20" for r in core.log if "/batches/" in r["path"])


def test_no_wait_leaves_the_verdict_for_the_next_run_without_sending_again(rig, core, tmp_path, fake_time):
    core.add_task("europepmc")
    rig.run(max_tasks=1, wait_verdict=False)
    pending = rig.box.pending(core.url)
    assert len(pending) == 1 and pending[0]["batch_id"] and rig.runner.stats.batches == 0
    second = Rig(core, "sbs_test_token_0123456789", tmp_path, fake_time)
    stats = second.run(once=True)
    assert stats.batches == 1 and len(rig.posts("/batches")) == 1 and second.box.pending(core.url) == []


def test_a_server_that_does_not_check_batches_stops_the_run_with_a_clear_message(rig, core, capsys):
    """Regression: with the site's background job stopped, the program kept harvesting and piling batches on the server for hours, saying only "still being checked"."""
    core.polls_before_final = 10**9                                    # the verdict never becomes final
    for _ in range(6):
        core.add_task("europepmc")
    stats = rig.run()
    err = capsys.readouterr().err
    assert stats.stopped_reason == "server_not_checking"
    assert len(rig.posts("/batches")) == 3 and len(rig.box.pending(core.url)) == 3             # three batches, kept safely, no more work piled up
    assert "stopping" in err and "server's side" in err and "Nothing is lost" in err


def test_a_checked_batch_resets_the_count_of_unchecked_ones(rig, core):
    core.add_task("europepmc")
    core.add_task("europepmc")
    stats = rig.run(max_tasks=2)
    assert stats.batches == 2 and rig.runner.unchecked == 0 and stats.stopped_reason == "max_tasks"


def test_a_task_the_server_hands_out_again_is_not_done_twice(rig, core, capsys):
    """An older server kept handing out the task whose batch it had not settled yet: the program must not harvest and send it again."""
    core.polls_before_final = 10**9
    core.add_task("europepmc")
    rig.run(max_tasks=1)
    assert len(rig.posts("/batches")) == 1 and len(rig.box.pending(core.url)) == 1
    core.tasks.append(core.make_task(task_id=1))                       # the same task, offered again
    capsys.readouterr()
    rig.run(max_tasks=1)
    assert len(rig.posts("/batches")) == 1                             # nothing new was sent (the earlier batch was only asked about again)
    assert "not doing it twice" in capsys.readouterr().err


def test_the_run_says_how_many_crossref_papers_were_left_out_for_not_containing_the_phrase(rig, core, capsys):
    core.tasks.append(core.make_task("crossref", task_id=1, keyword="liquid hydrogen tank sloshing"))
    rig.run(max_tasks=1)
    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert "3 without the exact phrase left out" in out and "0 to send" in out


# ---------------------------------------------------------------------------------------------------- failures while sending
def test_an_expired_lease_costs_nothing_and_blames_nobody(rig, core, capsys):
    core.add_task("europepmc")
    core.expired_tasks.add(1)
    stats = rig.run(max_tasks=1)
    assert stats.expired == 1 and stats.batches == 0 and rig.box.pending(core.url) == []
    assert rig.box.history(core.url)[0]["status"] == "expired" and "nobody is blamed" in capsys.readouterr().err


def test_the_lease_lost_during_a_long_task_is_noticed_at_the_heartbeat(rig, core, capsys):
    core.lease_minutes = 1
    core.add_task("europepmc")
    core.expired_tasks.add(1)
    stats = rig.run(max_tasks=1)
    assert stats.expired == 1 and rig.posts("/batches") == [] and core.released == [] and "lease expired" in capsys.readouterr().err


def test_a_network_failure_keeps_the_batch_and_the_next_run_finishes_it(rig, core, tmp_path, fake_time):
    core.add_task("europepmc")
    core.fail.append({"match": "batches", "status": 503, "times": 99})
    stats = rig.run(max_tasks=1)
    pending = rig.box.pending(core.url)
    assert stats.batches == 0 and len(pending) == 1 and pending[0]["last_error"] and pending[0]["batch_id"] == ""
    core.fail.clear()
    again = Rig(core, "sbs_test_token_0123456789", tmp_path, fake_time)
    stats = again.run(once=True)
    assert stats.batches == 1 and again.box.pending(core.url) == []
    ids = {r["body"]["client_batch_id"] for r in core.requests_to("/batches") if r["method"] == "POST"}
    assert len(ids) == 1, "the resend must reuse the same client_batch_id"


def test_a_crash_between_sending_and_the_verdict_is_repaired_by_the_duplicate_answer(rig, core):
    """The batch reached the core but the program died before it noted the batch id: resending is answered with duplicate_batch."""
    core.add_task("europepmc")
    api = rig.api
    task = api.claim(capabilities=["harvest/1"])["tasks"][0]
    batch = {"client_batch_id": "crashed-run", "started_at": "x", "finished_at": "y", "pages_fetched": 1, "error": None,
             "records": [{"title": "A title that is long enough", "doi": "10.1234/z.1", "pmid": "", "abstract": "", "authors": "", "journal": "", "year": "2023", "language": "", "urls": []}]}
    api.submit_batch(task["id"], batch)
    rig.box.add(core.url, task["id"], batch, "europepmc", "microgravity")
    rig.runner.flush_outbox(RunOptions())
    assert rig.runner.stats.batches == 1 and len(core.batches) == 1 and rig.box.pending(core.url) == []


def test_a_batch_the_core_calls_malformed_is_dropped_not_retried_forever(rig, core, capsys):
    core.add_task("europepmc")
    core.fail.append({"match": "tasks/1/batches", "status": 422, "body": {"error": {"code": "invalid_payload", "message": "bad"}}, "times": 1})
    stats = rig.run(max_tasks=1)
    assert stats.batches == 0 and rig.box.pending(core.url) == [] and rig.box.history(core.url)[0]["reason"] == "invalid_payload"
    assert "refused the batch format" in capsys.readouterr().err


def test_pending_batches_are_finished_before_new_work(rig, core):
    core.add_task("europepmc", task_id=1)
    api = rig.api
    task = api.claim(capabilities=["harvest/1"])["tasks"][0]
    rig.box.add(core.url, task["id"], {"client_batch_id": "left-over", "records": []}, "europepmc", "k")
    core.add_task("crossref", task_id=2)
    stats = rig.run(max_tasks=1)
    assert stats.batches == 2
    first = [r["body"]["client_batch_id"] for r in core.log if r["method"] == "POST" and r["path"].endswith("/batches")]
    assert first[0] == "left-over"


# ---------------------------------------------------------------------------------------------------- the databases misbehave
def test_a_busy_database_gets_its_task_back_and_is_left_alone(rig, core):
    core.source_status["europepmc"] = 429
    core.add_task("europepmc", task_id=1)
    core.add_task("crossref", task_id=2)
    stats = rig.run(max_tasks=2)
    assert core.released[0]["task_id"] == 1 and core.released[0]["reason"] == "rate_limited_by_source"
    claims = [r["body"] for r in core.requests_to("tasks/claim")]
    assert claims[0]["skip_sources"] == [] and claims[1]["skip_sources"] == ["europepmc"]
    assert stats.released == 1 and stats.batches == 1


def test_a_database_that_is_down_is_released_as_unavailable(rig, core):
    core.source_status["crossref"] = 503
    core.add_task("crossref")
    rig.run(max_tasks=1)
    assert core.released[0]["reason"] == "source_unavailable" and rig.posts("/batches") == []


def test_a_task_of_a_kind_this_client_does_not_know_is_given_back(rig, core):
    core.ignore_capabilities = True
    core.add_task("europepmc", kind="embed/1")
    rig.run(max_tasks=1)
    assert core.released[0]["reason"] == "unsupported_kind" and core.source_log == []


def test_an_unknown_database_is_given_back(rig, core):
    core.add_task("scopus")
    rig.run(max_tasks=1)
    assert core.released[0]["reason"] == "unsupported_kind" and "scopus" in core.released[0]["detail"]


def test_a_task_asking_for_a_host_it_did_not_allow_is_never_obeyed(rig, core, capsys):
    core.add_task("europepmc", allowed_hosts=["example.org"])
    rig.run(max_tasks=1)
    assert core.source_log == [] and core.released[0]["reason"] == "error" and core.released[0]["detail"] == "host_not_allowed"
    assert "did not allow" in capsys.readouterr().err


# ---------------------------------------------------------------------------------------------------- limits and stopping
def test_dry_run_sends_nothing_and_leases_nothing(rig, core, capsys):
    core.add_task("europepmc")
    stats = rig.run(dry_run=True, max_tasks=1, show_payload=True)
    out = capsys.readouterr().out
    assert stats.tasks == 1 and "DRY RUN" in out and '"client_batch_id"' in out
    assert rig.posts("/batches") == [] and rig.posts("/release") == [] and rig.posts("/heartbeat") == [] and rig.box.pending(core.url) == []
    assert core.requests_to("tasks/claim")[0]["body"]["dry_run"] is True and len(core.tasks) == 1 and core.claimed == {}


def test_dry_run_does_not_touch_the_outbox_of_earlier_runs(rig, core):
    rig.box.add(core.url, 9, {"client_batch_id": "keep-me", "records": []})
    core.add_task("europepmc")
    rig.run(dry_run=True, max_tasks=1)
    assert [r["client_batch_id"] for r in rig.box.pending(core.url)] == ["keep-me"] and rig.posts("/batches") == []


def test_max_tasks_stops_and_leaves_the_rest(rig, core):
    for i in (1, 2, 3):
        core.add_task("europepmc", task_id=i)
    stats = rig.run(max_tasks=2)
    assert stats.tasks == 2 and stats.stopped_reason == "max_tasks" and len(core.tasks) == 1


def test_the_time_limit_is_honoured_while_waiting_for_work(rig, core, fake_time):
    stats = rig.run(duration=100)
    assert stats.stopped_reason == "time_up" and len(core.requests_to("tasks/claim")) == 1


def test_once_asks_once(rig, core):
    stats = rig.run(once=True)
    assert stats.stopped_reason == "no_tasks" and len(core.requests_to("tasks/claim")) == 1


def test_the_daily_goal_ends_the_run_politely(rig, core, capsys):
    core.claim_reason = "daily_limit"
    stats = rig.run()
    assert stats.stopped_reason == "daily_limit" and "thank you" in capsys.readouterr().out


def test_a_paused_account_waits_or_stops_with_once(rig, core):
    core.account_status = "paused"
    assert rig.run(once=True).stopped_reason == "paused"


def test_waiting_for_work_sleeps_in_short_slices_and_retries(rig, core, fake_time):
    core.claim_reason = "no_tasks"
    rig.run(duration=2000)
    assert len(core.requests_to("tasks/claim")) >= 3 and max(fake_time.sleeps) <= 600


def test_a_suspended_or_unconsented_account_refuses_to_start(rig, core):
    core.account_status = "suspended"
    with pytest.raises(errors.Suspended):
        rig.run(once=True)
    core.account_status = "consent_needed"
    with pytest.raises(errors.NotConsented):
        rig.run(once=True)
    assert core.requests_to("tasks/claim") == []


def test_asked_to_slow_down_it_does_not_crash(rig, core):
    core.fail.append({"match": "tasks/claim", "status": 429, "body": {"error": {"code": "rate_limited", "retry_after": 5}}, "times": 1})
    assert rig.run(once=True).stopped_reason == "slow_down"


def test_the_claim_rhythm_follows_the_server_limit(core, token, tmp_path, fake_time):
    core.claim_min_interval_s = 4
    core.add_task("europepmc", task_id=1)
    core.add_task("europepmc", task_id=2)
    rig = Rig(core, token, tmp_path, fake_time)
    rig.run(max_tasks=2)
    assert sum(fake_time.sleeps) >= 4 - 1e-6


def test_ctrl_c_gives_the_task_back_and_ends_cleanly(core, token, tmp_path, fake_time):
    core.add_task("europepmc", pages=3)
    rig = Rig(core, token, tmp_path, fake_time, stop=lambda: len(core.source_log) >= 1)
    stats = rig.run(max_tasks=5)
    assert stats.stopped_reason == "interrupted" and core.released[0]["reason"] == "user_stopped" and rig.box.pending(core.url) == [] and rig.posts("/batches") == []


def test_stopping_before_starting_does_nothing(core, token, tmp_path, fake_time):
    core.add_task("europepmc")
    rig = Rig(core, token, tmp_path, fake_time, stop=lambda: True)
    stats = rig.run(max_tasks=5)
    assert stats.stopped_reason == "interrupted" and core.requests_to("tasks/claim") == []


# ---------------------------------------------------------------------------------------------------- options reach the core
def test_focus_and_skips_are_sent(rig, core):
    rig.run(once=True, focus=["radiation", "plants"], focus_only=True, skip_sources=["openalex"])
    assert core.requests_to("tasks/claim")[0]["body"] | {} == {"capabilities": ["harvest/1"], "focus": ["radiation", "plants"], "focus_only": True,
                                                              "skip_sources": ["openalex"], "max_tasks": 1, "dry_run": False}


def test_a_long_task_keeps_its_lease_alive(core, token, tmp_path, fake_time):
    core.lease_minutes = 1
    core.add_task("europepmc")
    rig = Rig(core, token, tmp_path, fake_time)
    rig.run(max_tasks=1)
    assert core.heartbeats == [1, 1]


def test_a_comfortable_lease_needs_no_heartbeat(rig, core):
    core.add_task("europepmc")
    rig.run(max_tasks=1)
    assert core.heartbeats == []


def test_the_batch_size_is_capped_by_the_server_and_by_the_person(core, token, tmp_path, fake_time):
    core.max_records_per_batch = 1
    core.add_task("crossref", task_id=1)
    Rig(core, token, tmp_path, fake_time).run(max_tasks=1)
    assert len(core.requests_to("tasks/1/batches")[0]["body"]["records"]) == 1
    core.max_records_per_batch = 1000
    core.add_task("crossref", task_id=2)
    Rig(core, token, tmp_path / "b", fake_time).run(max_tasks=1, max_batch_records=2)
    assert len(core.requests_to("tasks/2/batches")[0]["body"]["records"]) == 2


def test_nothing_found_still_closes_the_task_with_an_empty_batch(rig, core):
    core.add_task("pubmed", page_size=2)
    core.fail.clear()
    # a database with no results: ask for a page beyond the end
    core.tasks[0]["request"]["url"] = core.tasks[0]["request"]["url"].replace("retstart=0", "retstart=50")
    rig.run(max_tasks=1)
    assert rig.posts("/batches")[0]["body"]["records"] == []


# ---------------------------------------------------------------------------------------------------- machine-readable output
def test_json_mode_emits_one_event_per_batch(core, token, tmp_path, fake_time, capsys):
    core.add_task("europepmc")
    Rig(core, token, tmp_path, fake_time, json_mode=True).run(max_tasks=1)
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [e["event"] for e in events] == ["batch"] and events[0]["status"] == "accepted" and events[0]["points"] == 31


def test_openalex_without_a_key_still_contributes_its_first_page_and_tells_the_person(rig, core, capsys):
    core.paging_needs_key.add("openalex")
    core.add_task("openalex", pages=5)
    stats = rig.run(max_tasks=1)
    assert stats.batches == 1 and len(rig.posts("/batches")[0]["body"]["records"]) == 2 and rig.posts("/batches")[0]["body"]["pages_fetched"] == 2
    assert "OPENALEX_API_KEY" in capsys.readouterr().err


def test_a_database_that_wants_a_key_is_left_alone_for_six_hours(rig, core, capsys):
    core.source_status["openalex"] = 401
    core.add_task("openalex", task_id=1)
    core.add_task("crossref", task_id=2)
    stats = rig.run(max_tasks=2)
    assert core.released[0]["task_id"] == 1 and core.released[0]["reason"] == "source_unavailable"
    assert core.requests_to("tasks/claim")[1]["body"]["skip_sources"] == ["openalex"] and stats.batches == 1
    err = capsys.readouterr().err
    assert "wants an API key" in err and "6 hours" in err                      # (the variable name is suggested for the real host, see test_fetcher)
