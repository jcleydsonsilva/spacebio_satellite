"""The command line, end to end, against the mock core. Exit codes are part of the contract with scripts that call this program."""
import json
import os
import stat

import keyring
import pytest
from keyring.backends import fail

from spacebio_satellite import cli, errors
from spacebio_satellite.api import ApiClient
from spacebio_satellite.config import get_paths
from spacebio_satellite.outbox import Outbox

main = cli.main


def out_err(capsys):
    captured = capsys.readouterr()
    return captured.out, captured.err


def json_lines(text):
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# ---------------------------------------------------------------------------------------------------- login, status, logout
def test_login_status_logout(served, fake_time, capsys, memory_keyring):
    assert main(["login", "--no-browser", "--label", "Home laptop"]) == 0
    out, _ = out_err(capsys)
    assert "Logged in to " in out and "system keyring" in out
    assert (("spacebio-satellite", served.url) in memory_keyring.items) and len(served.tokens) == 1

    assert main(["status"]) == 0
    out, _ = out_err(capsys)
    assert "active · tier newcomer" in out and "400 left of 400 daily" in out and "(none: any task)" in out

    assert main(["logout"]) == 0
    out, _ = out_err(capsys)
    assert "revoked on the server" in out and served.tokens == {} and memory_keyring.items == {}
    assert get_paths().settings_file.exists() and served.url not in json.loads(get_paths().settings_file.read_text())["devices"]


def test_login_remembers_the_device_without_the_token(served, fake_time, memory_keyring):
    main(["login", "--no-browser", "--label", "Home laptop"])
    settings_text = get_paths().settings_file.read_text()
    saved = json.loads(settings_text)
    assert saved["default_server"] == served.url and saved["devices"][served.url]["label"] == "Home laptop" and saved["devices"][served.url]["token_storage"] == "keyring"
    secret = memory_keyring.items[("spacebio-satellite", served.url)]
    assert secret not in settings_text and oct(stat.S_IMODE(os.stat(get_paths().settings_file).st_mode)) == "0o600"


def test_login_in_json_mode_prints_events_and_a_result(served, fake_time, capsys):
    assert main(["--json", "login", "--no-browser", "--label", "x"]) == 0
    lines = json_lines(capsys.readouterr().out)
    assert lines[0]["event"] == "login_code" and lines[0]["user_code"] == "WDJB-MJHT" and lines[-1]["logged_in"] is True and lines[-1]["token_storage"] == "keyring"
    assert not any("access_token" in line for line in lines)


def test_login_refused_has_its_own_exit_code(served, fake_time, capsys):
    served.deny = True
    assert main(["login", "--no-browser"]) == 4
    assert "refused" in out_err(capsys)[1]


def test_login_without_a_secure_keyring_explains_the_options(served, fake_time, capsys):
    keyring.set_keyring(fail.Keyring())
    assert main(["login", "--no-browser"]) == 1
    err = out_err(capsys)[1]
    assert "SPACEBIO_SATELLITE_TOKEN" in err and "--allow-plaintext-token" in err and not get_paths().token_file.exists()


def test_login_with_the_plaintext_flag_stores_a_private_file_and_warns(served, fake_time, capsys):
    keyring.set_keyring(fail.Keyring())
    assert main(["login", "--no-browser", "--allow-plaintext-token"]) == 0
    out = out_err(capsys)[0]
    assert "less safe" in out and stat.S_IMODE(os.stat(get_paths().token_file).st_mode) == 0o600
    assert main(["status"]) == 0                                       # and it is really used afterwards


def test_not_logged_in_is_exit_3_with_the_next_step(served, capsys):
    assert main(["status"]) == 3
    assert "spacebio-satellite login" in out_err(capsys)[1]
    assert main(["run", "--once"]) == 3
    assert main(["focus"]) == 3


def test_a_revoked_or_expired_token_is_exit_3(logged_in, capsys):
    logged_in.tokens.clear()
    assert main(["status"]) == 3 and main(["run", "--once"]) == 3
    assert "login" in out_err(capsys)[1]


def test_logout_when_not_logged_in_is_fine(served, capsys):
    assert main(["logout"]) == 0 and "not logged in" in out_err(capsys)[0].lower()


def test_logout_local_only_does_not_call_the_server(logged_in):
    before = len(logged_in.requests_to("revoke"))
    assert main(["logout", "--local-only"]) == 0
    assert len(logged_in.requests_to("revoke")) == before and len(logged_in.tokens) == 1


def test_logout_with_the_server_down_still_removes_the_local_token(logged_in, capsys, memory_keyring):
    logged_in.stop()
    assert main(["logout"]) == 0
    out, err = out_err(capsys)
    assert "could not revoke" in err and memory_keyring.items == {} and "Logged out." in out


def test_the_environment_token_works_without_any_login(served, token, capsys):
    os.environ["SPACEBIO_SATELLITE_TOKEN"] = token
    try:
        assert main(["status"]) == 0
        assert main(["logout", "--local-only"]) == 0
        assert "unset it" in out_err(capsys)[1]
    finally:
        del os.environ["SPACEBIO_SATELLITE_TOKEN"]


def test_the_server_flag_beats_the_environment(core, token, monkeypatch, capsys):
    monkeypatch.setenv("SPACEBIO_SATELLITE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("SPACEBIO_SATELLITE_TOKEN", token)
    assert main(["--server", core.url, "status"]) == 0
    assert main(["status", "--server", core.url]) == 0                  # options also work after the command
    assert main(["status"]) == 6


# ---------------------------------------------------------------------------------------------------- run
def test_run_end_to_end(logged_in, capsys):
    logged_in.add_task("europepmc", task_id=1)
    logged_in.add_task("crossref", task_id=2)
    assert main(["run", "--max-tasks", "2"]) == 0
    out = out_err(capsys)[0]
    assert out.count("accepted:") == 2 and "Done (max_tasks) in " in out and "2 task(s), 6 new paper(s)" in out and "+62 points" in out


def test_run_in_json_mode_gives_events_and_a_final_result(logged_in, capsys):
    logged_in.add_task("openalex")
    assert main(["run", "--json", "--once"]) == 0
    lines = json_lines(capsys.readouterr().out)
    assert [l["event"] for l in lines[:-1]] == ["batch"] and lines[-1]["done"] is True and lines[-1]["new"] == 3 and lines[-1]["dry_run"] is False


def test_dry_run_defaults_to_one_task_and_sends_nothing(logged_in, capsys):
    for i in (1, 2, 3):
        logged_in.add_task("europepmc", task_id=i)
    assert main(["run", "--dry-run"]) == 0
    out = out_err(capsys)[0]
    assert "DRY RUN" in out and "Done (max_tasks) in " in out and "1 task(s)" in out
    assert not [r for r in logged_in.log if r["method"] == "POST" and r["path"].endswith("/batches")] and len(logged_in.tasks) == 3


def test_show_payload_prints_the_exact_batch(logged_in, capsys):
    logged_in.add_task("europepmc")
    assert main(["run", "--dry-run", "--show-payload"]) == 0
    out = out_err(capsys)[0]
    assert '"client_batch_id"' in out and '"records"' in out and "10.1038/s41526-023-00336-6" in out


def test_run_options_reach_the_core(logged_in):
    assert main(["run", "--once", "--focus", "radiation,plants", "--focus", "mice", "--focus-only", "--skip-source", "openalex", "--skip-source", "crossref"]) == 0
    body = logged_in.requests_to("tasks/claim")[-1]["body"]
    assert body["focus"] == ["radiation", "plants", "mice"] and body["focus_only"] is True and body["skip_sources"] == ["crossref", "openalex"]


@pytest.mark.parametrize("argv", [["run", "--for", "30"], ["run", "--for", "banana"], ["run", "--for", "0m"], ["run", "--max-tasks", "0"]])
def test_wrong_usage_is_exit_2(served, argv, capsys):
    assert main(argv) == 2
    assert capsys.readouterr().err.startswith("error:")


def test_an_old_client_is_exit_5_with_the_command_to_upgrade(logged_in, capsys):
    logged_in.min_client_version = "9.0.0"
    assert main(["run", "--once"]) == 5
    assert "pipx upgrade spacebio-satellite" in out_err(capsys)[1]


def test_a_server_that_is_down_is_exit_6(logged_in, capsys):
    logged_in.stop()
    assert main(["run", "--once"]) == 6
    assert "doctor" in out_err(capsys)[1]


def test_a_suspended_account_is_exit_4(logged_in, capsys):
    logged_in.account_status = "suspended"
    assert main(["run", "--once"]) == 4
    assert "suspended" in out_err(capsys)[1].lower()


def test_a_suspension_during_a_run_still_prints_what_was_done(logged_in, capsys):
    logged_in.add_task("europepmc")
    logged_in.verdict_fn = lambda b: {"status": "rejected", "reason": "verification_failed", "counts": {}, "points": 0, "suspended": True}
    assert main(["run", "--max-tasks", "1"]) == 4
    out, err = out_err(capsys)
    assert "Done (error)" in out and "suspended" in err.lower()


def test_ctrl_c_in_a_command_is_exit_130(served, monkeypatch, capsys):
    def boom(args, out):
        raise KeyboardInterrupt

    monkeypatch.setitem(cli.COMMANDS, "status", boom)
    assert main(["status"]) == 130
    assert "Interrupted" in out_err(capsys)[1]


def test_quiet_hides_the_chatter_but_not_the_problems(logged_in, capsys):
    logged_in.add_task("europepmc")
    logged_in.verdict_fn = lambda b: {"status": "rejected", "reason": "unconfirmed", "counts": {}, "points": 0, "suspended": False}
    assert main(["run", "-q", "--max-tasks", "1"]) == 0
    out, err = out_err(capsys)
    assert out == "" and "rejected" in err


# ---------------------------------------------------------------------------------------------------- focus, outbox, history
def test_focus_set_show_clear(logged_in, capsys):
    assert main(["focus", "radiation", "microgravity,plants"]) == 0
    assert logged_in.focus == ["radiation", "microgravity", "plants"] and "radiation, microgravity, plants" in out_err(capsys)[0]
    assert main(["focus"]) == 0 and "radiation" in out_err(capsys)[0]
    assert main(["focus", "--clear"]) == 0 and logged_in.focus == [] and "none" in out_err(capsys)[0]
    assert main(["focus", "--clear", "radiation"]) == 2


def seed_outbox(core, n=1):
    box = Outbox(get_paths().outbox_file)
    for i in range(n):
        box.add(core.url, 100 + i, {"client_batch_id": f"left-{i}", "records": [{"title": "x" * 20, "doi": f"10.1234/{i}"}]}, "europepmc", "microgravity")
    return box


def test_outbox_lists_what_is_waiting(logged_in, capsys):
    seed_outbox(logged_in, 2)
    assert main(["outbox"]) == 0
    out = out_err(capsys)[0]
    assert "2 batch(es) waiting" in out and "task 100" in out and "outbox --flush" in out
    assert main(["--json", "outbox"]) == 0 and len(json.loads(capsys.readouterr().out)["pending"]) == 2


def test_outbox_flush_sends_what_is_waiting(logged_in, capsys):
    logged_in.add_task("europepmc", task_id=100)
    api = ApiClient(logged_in.url, next(iter(logged_in.tokens)), sleep=lambda s: None)
    api.claim(capabilities=["harvest/1"])
    seed_outbox(logged_in, 1)
    assert main(["outbox", "--flush"]) == 0
    assert "Accepted 1 batch(es); 0 still waiting" in out_err(capsys)[0] and Outbox(get_paths().outbox_file).pending(logged_in.url) == []


def test_outbox_clear_asks_first_in_scripts(logged_in, capsys):
    box = seed_outbox(logged_in, 2)
    assert main(["outbox", "--clear"]) == 2 and len(box.pending(logged_in.url)) == 2
    assert main(["outbox", "--clear", "--yes"]) == 0 and box.pending(logged_in.url) == [] and "Removed 2" in out_err(capsys)[0]


def test_history_shows_the_verdicts(logged_in, capsys):
    logged_in.add_task("europepmc", task_id=1)
    logged_in.add_task("crossref", task_id=2)
    main(["run", "--max-tasks", "2"])
    capsys.readouterr()
    assert main(["history"]) == 0
    lines = out_err(capsys)[0].strip().splitlines()
    assert len(lines) == 2 and "accepted" in lines[0] and "crossref" in lines[0] and "europepmc" in lines[1]
    assert main(["history", "-n", "1"]) == 0 and len(out_err(capsys)[0].strip().splitlines()) == 1


def test_history_when_empty(logged_in, capsys):
    assert main(["history"]) == 0 and "No batches yet" in out_err(capsys)[0]


def test_status_reports_the_local_totals_and_a_newer_version(logged_in, capsys):
    logged_in.latest_client_version = "9.9.9"
    logged_in.add_task("europepmc")
    main(["run", "--max-tasks", "1"])
    capsys.readouterr()
    assert main(["status"]) == 0
    out = out_err(capsys)[0]
    assert "1 batch(es): 3 new" in out and "A newer version is available: 9.9.9" in out


def test_status_json_is_complete(logged_in, capsys):
    assert main(["--json", "status"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "active" and data["points_left"] == 400 and data["outbox_pending"] == 0 and data["server"] == logged_in.url and data["update_available"] is False


# ---------------------------------------------------------------------------------------------------- the small commands
def test_privacy_says_what_leaves_the_computer(capsys):
    assert main(["privacy"]) == 0
    out = out_err(capsys)[0]
    assert "WHAT LEAVES YOUR COMPUTER" in out and "IP address" in out and "cannot delete, merge or edit" in out and "--dry-run" in out


def test_version_and_flag(capsys):
    assert main(["version"]) == 0 and "spacebio-satellite 0.1.0" in out_err(capsys)[0]
    assert main(["--version"]) == 0 and "0.1.0" in out_err(capsys)[0]


def test_no_command_prints_help_and_exit_2(capsys):
    assert main([]) == 2
    assert "first steps" in out_err(capsys)[0]
    assert main(["bogus"]) == 2


def test_help_mentions_every_command(capsys):
    assert main(["--help"]) == 0
    out = out_err(capsys)[0]
    for name in cli.COMMANDS:
        assert name in out


# ---------------------------------------------------------------------------------------------------- doctor
def test_doctor_all_green(logged_in, capsys, monkeypatch):
    monkeypatch.setenv("NCBI_API_KEY", "SUPER-SECRET-NCBI-KEY")
    assert main(["doctor"]) == 0
    out = out_err(capsys)[0]
    assert "[ok  ] server" in out and "[ok  ] login" in out and "NCBI_API_KEY" in out and "SUPER-SECRET-NCBI-KEY" not in out
    assert "FAIL" not in out and "WARN" not in out


def test_doctor_when_not_logged_in_warns_but_is_not_a_failure(served, capsys):
    assert main(["doctor"]) == 0
    assert "[WARN] login" in out_err(capsys)[0]


def test_doctor_with_the_server_down_fails(served, capsys):
    served.stop()
    assert main(["doctor"]) == 1
    assert "[FAIL] server" in out_err(capsys)[0]


def test_doctor_flags_an_insecure_keyring_and_a_clock_far_from_the_server(served, capsys, monkeypatch):
    keyring.set_keyring(fail.Keyring())
    monkeypatch.setattr(cli, "DEPS", cli.Deps(sleep=lambda s: None, clock=lambda: 0.0, now=lambda: cli.parse_iso("2020-01-01T00:00:00Z"), opener=lambda u: True))
    assert main(["doctor"]) == 0
    out = out_err(capsys)[0]
    assert "[WARN] keyring" in out and "[WARN] clock" in out


def test_doctor_fails_for_a_client_that_is_too_old(served, capsys):
    served.min_client_version = "9.0.0"
    assert main(["doctor"]) == 1 and "[FAIL] version" in out_err(capsys)[0]


def test_doctor_json(logged_in, capsys):
    assert main(["--json", "doctor"]) == 0
    checks = json.loads(capsys.readouterr().out)["checks"]
    assert {c["check"] for c in checks} >= {"python", "keyring", "server", "version", "login", "outbox"}


# ---------------------------------------------------------------------------------------------------- secrets
def test_the_token_never_appears_in_any_output(served, fake_time, capsys, memory_keyring):
    served.add_task("europepmc")
    outputs = []
    for argv in (["login", "--no-browser"], ["status"], ["run", "--max-tasks", "1", "-v"], ["--json", "status"], ["doctor"], ["history"], ["outbox"], ["logout"]):
        main(argv)
        outputs.append(out_err(capsys))
        if argv[0] == "login":
            secret = memory_keyring.items[("spacebio-satellite", served.url)]
    flat = "".join(o + e for o, e in outputs)
    assert secret.startswith("sbs_") and secret not in flat
    assert secret not in "".join(p.read_text() for p in get_paths().config_dir.rglob("*") if p.is_file() and p.name != "token.json")


def test_error_classes_have_distinct_documented_exit_codes():
    codes = {errors.NotLoggedIn: 3, errors.InvalidToken: 3, errors.Suspended: 4, errors.NotConsented: 4, errors.UpgradeRequired: 5, errors.NetworkError: 6, errors.UsageError: 2,
             errors.StopRequested: 130, errors.LoginDenied: 4, errors.Unavailable: 6}
    for cls, code in codes.items():
        assert cls.exit_code == code, cls


# ---- a computer that waits for an administrator's approval
def test_login_says_the_computer_waits_for_approval_and_status_tells_when_it_is_approved(served, fake_time, capsys, memory_keyring):
    served.approval_seconds = float("inf")
    assert main(["login", "--no-browser", "--label", "Lab desktop"]) == 0
    out, _ = out_err(capsys)
    assert "waits for approval by a SpaceBio administrator" in out and "Next: spacebio-satellite run" not in out

    assert main(["status"]) == 0                                    # asking is allowed while waiting
    out, _ = out_err(capsys)
    assert "waiting for approval by a SpaceBio administrator" in out

    served.approve_all()
    assert main(["status"]) == 0
    out, _ = out_err(capsys)
    assert "waiting for approval" not in out and "active · tier newcomer" in out


def test_run_while_waiting_for_approval_is_exit_4_with_a_clear_message_and_claims_nothing(served, fake_time, capsys, memory_keyring):
    served.approval_seconds = float("inf")
    served.add_task("europepmc")
    assert main(["login", "--no-browser"]) == 0
    out_err(capsys)
    assert main(["run", "--once"]) == 4
    err = out_err(capsys)[1].lower()
    assert "waiting for approval" in err and "approve" in err
    assert not served.requests_to("tasks/claim") and len(served.tasks) == 1

    served.approve_all()
    assert main(["run", "--once"]) == 0
    assert served.requests_to("tasks/claim")


def test_the_json_result_of_login_and_status_carries_the_approval_state(served, fake_time, capsys, memory_keyring):
    served.approval_seconds = float("inf")
    assert main(["--json", "login", "--no-browser"]) == 0
    assert json_lines(out_err(capsys)[0])[-1]["approval"] == "pending"
    assert main(["--json", "status"]) == 0
    assert json_lines(out_err(capsys)[0])[-1]["approval"] == "pending"
    served.approve_all()
    assert main(["--json", "status"]) == 0
    assert json_lines(out_err(capsys)[0])[-1]["approval"] == "approved"


def test_the_core_error_for_a_waiting_computer_maps_to_its_own_exception():
    from spacebio_satellite.errors import PendingApproval, error_from_response

    error = error_from_response(403, {"error": {"code": "pending_approval", "message": "wait"}})
    assert isinstance(error, PendingApproval) and error.exit_code == 4 and "administrator" in error.hint


def test_a_server_that_answers_html_is_named_in_the_error_with_a_hint(fake_time, capsys, memory_keyring):
    """A site that does not offer the Satellite API (it answers its home page) must say which server it was, not just 'bad response'."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Page(BaseHTTPRequestHandler):
        def do_GET(self):
            self._send()

        do_POST = do_GET

        def _send(self):
            body = b"<!DOCTYPE html><html><body>home</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        assert main(["--server", url, "login", "--no-browser", "--label", "x"]) == 1
    finally:
        server.shutdown()
    err = out_err(capsys)[1]
    assert url in err and "HTTP 200, text/html" in err and "--server" in err and "SPACEBIO_SATELLITE_URL" in err
