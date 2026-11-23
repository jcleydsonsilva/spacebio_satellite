"""The command line: ``spacebio-satellite <command>`` (also ``sbsat``). See ``spacebio-satellite --help``."""
from __future__ import annotations

import argparse
import os
import platform
import signal
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass

from . import RECORD_SCHEMAS, TASK_KINDS, __version__, auth
from .api import ApiClient
from .config import ENV_TOKEN, Device, Paths, Settings, get_paths, resolve_server
from .credentials import TokenStore, keyring_description, keyring_is_secure
from .errors import ApiError, InvalidToken, NetworkError, NotLoggedIn, SatelliteError, UsageError
from .outbox import Outbox
from .output import Output
from .privacy import render as privacy_lines
from .runner import RunOptions, Runner
from .util import human_duration, iso, parse_duration, parse_iso, parse_version, utcnow

DESCRIPTION = """Lend a little of your computer to SpaceBio: ask the core for a small task, search the public scientific databases
from your computer, and send back the papers you found. The core verifies everything. Nothing here can delete or change anything."""
EPILOG = """first steps:
  spacebio-satellite privacy         read what leaves your computer
  spacebio-satellite login           connect this computer (approve in your browser)
  spacebio-satellite run --dry-run   see what a task would send, sending nothing
  spacebio-satellite run --for 30m   work for half an hour

exit codes: 0 ok · 1 failure · 2 wrong usage · 3 sign in needed · 4 not allowed · 5 update needed · 6 network/server down · 130 stopped by you"""


@dataclass
class Deps:
    """What tests replace: time and the browser."""

    sleep: object = time.sleep
    clock: object = time.monotonic
    now: object = utcnow
    opener: object = webbrowser.open


DEPS = Deps()


# ==================================================================================================== context
class Context:
    def __init__(self, args):
        self.paths: Paths = get_paths()
        self.settings = Settings.load(self.paths)
        self.server = resolve_server(getattr(args, "server", None), self.settings)
        self.store = TokenStore(self.paths)

    def token(self):
        token, where = self.store.get(self.server)
        if not token:
            raise NotLoggedIn("You are not logged in.")
        return token, where

    def api(self, *, authed=True):
        token = self.token()[0] if authed else ""
        return ApiClient(self.server, token, sleep=DEPS.sleep)

    def outbox(self):
        return Outbox(self.paths.outbox_file)


def _split_words(values):
    out = []
    for value in values or []:
        for word in str(value).split(","):
            word = word.strip()
            if word and word.lower() not in [w.lower() for w in out]:
                out.append(word)
    return out


# ==================================================================================================== commands
def cmd_login(args, out):
    ctx = Context(args)
    label = (args.label or "").strip()
    if not label:
        label = f"{platform.system() or 'My'} computer"
        if sys.stdin.isatty() and sys.stdout.isatty() and not out.json_mode:
            typed = input(f"A name for this computer [{label}]: ").strip()
            label = typed or label
    api = ctx.api(authed=False)
    result = auth.login(api, out, label=label, open_browser=not args.no_browser, sleep=DEPS.sleep, clock=DEPS.clock, opener=DEPS.opener)
    where = ctx.store.set(ctx.server, result["access_token"], allow_plaintext=args.allow_plaintext_token)
    ctx.settings.default_server = ctx.server
    ctx.settings.devices[ctx.server] = Device(device_id=str(result.get("device_id", "")), label=label, token_storage=where,
                                              token_expires_at=str(result.get("expires_at", "")), logged_in_at=iso(DEPS.now()))
    ctx.settings.save(ctx.paths)
    lines = [f"Logged in to {ctx.server} as '{label}'.", f"The token is stored in your {'system keyring' if where == 'keyring' else 'private file (' + str(ctx.paths.token_file) + ')'}."]
    if where == "file":
        lines.append("Warning: a plain file is less safe than a keyring. Anyone who can read your files can use the token.")
    if ENV_TOKEN in os.environ:
        lines.append(f"Note: {ENV_TOKEN} is set in your environment and will be used instead of the stored token.")
    lines.append("Next: spacebio-satellite run --dry-run")
    out.result({"logged_in": True, "server": ctx.server, "device_id": result.get("device_id"), "label": label, "token_storage": where,
                "expires_at": result.get("expires_at")}, lines)
    return 0


def cmd_logout(args, out):
    ctx = Context(args)
    token, where = ctx.store.get(ctx.server)
    if not token:
        out.result({"logged_in": False, "server": ctx.server}, ["You are not logged in."])
        return 0
    revoked = False
    if not args.local_only:
        try:
            ApiClient(ctx.server, token, sleep=DEPS.sleep, retries=1).revoke()
            revoked = True
        except InvalidToken:
            revoked = True                                     # it was already dead on the server: nothing left to revoke
        except (NetworkError, ApiError) as exc:
            out.warn(f"could not revoke this device on the server ({exc}). The token is removed here; revoke the device on the SpaceBio website too.")
    if where == "env":
        out.warn(f"The token comes from {ENV_TOKEN}; unset it in your shell.")
    else:
        ctx.store.delete(ctx.server)
    ctx.settings.devices.pop(ctx.server, None)
    ctx.settings.save(ctx.paths)
    pending = len(ctx.outbox().pending(ctx.server))
    lines = ["Logged out." + (" This device was revoked on the server." if revoked else "")]
    if pending:
        lines.append(f"{pending} unsent batch(es) remain in the local outbox (spacebio-satellite outbox --clear removes them).")
    out.result({"logged_out": True, "revoked": revoked, "outbox_pending": pending}, lines)
    return 0


def cmd_status(args, out):
    ctx = Context(args)
    api = ctx.api()
    meta = api.meta()
    me = api.me()
    device = ctx.settings.device(ctx.server)
    outbox = ctx.outbox()
    pending = len(outbox.pending(ctx.server))
    totals = outbox.totals(ctx.server)
    latest = meta.get("latest_client_version") or ""
    newer = bool(latest) and parse_version(latest) > parse_version(__version__)
    data = {"server": ctx.server, "client_version": __version__, "api_version": api.api_version, "latest_client_version": latest, "update_available": newer,
            "status": me.get("status"), "tier": me.get("tier"), "trust": me.get("trust"), "points_total": me.get("points_total"), "points_today": me.get("points_today"),
            "points_left": me.get("points_left"), "daily_points": me.get("daily_points"), "tasks_last_hour": me.get("tasks_last_hour"),
            "max_tasks_per_hour": me.get("max_tasks_per_hour"), "focus": me.get("focus") or [], "device": me.get("device") or {"label": device.label},
            "token_expires": api.token_expires or (me.get("device") or {}).get("expires_at") or device.token_expires_at, "outbox_pending": pending, "local_totals": totals}
    lines = [
        f"Server        {ctx.server}  (API {api.api_version or '?'})",
        f"Account       {me.get('status')} · tier {me.get('tier')} · trust {me.get('trust')}",
        f"Points        {me.get('points_total')} total · {me.get('points_today')} today · {me.get('points_left')} left of {me.get('daily_points')} daily",
        f"Work          {me.get('tasks_last_hour')} task(s) in the last hour (up to {me.get('max_tasks_per_hour')})",
        f"Focus         {', '.join(me.get('focus') or []) or '(none: any task)'}",
        f"This device   {(me.get('device') or {}).get('label') or device.label or '?'}",
        f"Outbox        {pending} batch(es) waiting to be sent",
        f"Seen locally  {totals['batches']} batch(es): {totals['new']} new, {totals['completed']} completed, {totals['rejected']} rejected",
    ]
    if newer:
        lines.append(f"A newer version is available: {latest} (you have {__version__}).")
    if me.get("status") == "paused":
        lines.append("Contributions are paused on the website.")
    out.result(data, lines)
    return 0


def _stop_flag():
    """Ctrl-C (or SIGTERM) asks to stop at the next safe point; a second one stops at once."""
    event = threading.Event()
    previous = {}

    def handler(signum, frame):
        if event.is_set():
            raise KeyboardInterrupt
        event.set()
        print("\nStopping after the current step... (press Ctrl-C again to stop now)", file=sys.stderr, flush=True)

    def install():
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                previous[sig] = signal.signal(sig, handler)
            except (ValueError, OSError):                       # not the main thread (tests) or not supported
                pass

    def restore():
        for sig, old in previous.items():
            try:
                signal.signal(sig, old)
            except (ValueError, OSError):
                pass

    return event, install, restore


def cmd_run(args, out):
    ctx = Context(args)
    try:
        duration = parse_duration(args.duration) if args.duration else None
    except ValueError as exc:
        raise UsageError(str(exc)) from exc
    if args.max_tasks is not None and args.max_tasks < 1:
        raise UsageError("--max-tasks must be at least 1")
    api = ctx.api()
    auth.check_client_version(api.meta())
    options = RunOptions(duration=duration, max_tasks=args.max_tasks, once=args.once, dry_run=args.dry_run, show_payload=args.show_payload,
                         focus=_split_words(args.focus) or None, focus_only=args.focus_only, skip_sources=_split_words(args.skip_source),
                         wait_verdict=not args.no_wait, max_batch_records=args.max_batch_records)
    if options.dry_run and options.max_tasks is None:
        options.max_tasks = 1
    if not options.dry_run and options.max_tasks is None and duration is None and not options.once:
        out.say("No limit given: working until you press Ctrl-C. (Use --for 30m or --max-tasks 5 to stop by itself.)")
    event, install, restore = _stop_flag()
    runner = Runner(api, ctx.outbox(), ctx.server, out, sleep=DEPS.sleep, clock=DEPS.clock, now=DEPS.now, stop=event.is_set)
    install()
    error = None
    began = DEPS.clock()
    try:
        runner.run(options)
    except KeyboardInterrupt:
        runner.stats.stopped_reason = "interrupted"
    except SatelliteError as exc:                               # still tell the person what was done before it failed
        runner.stats.stopped_reason = "error"
        error = exc
    finally:
        restore()
    done = runner.stats
    elapsed = DEPS.clock() - began
    lines = [f"Done ({done.stopped_reason or 'finished'}) in {human_duration(elapsed)}: {done.tasks} task(s), {done.new} new paper(s), {done.completed} completed, +{done.points} points"
             + (f", {done.rejected} rejected" if done.rejected else "")]
    out.result({"done": error is None, **done.as_dict(), "elapsed_s": int(elapsed), "dry_run": options.dry_run}, lines)
    if error is not None:
        raise error
    return 130 if done.stopped_reason == "interrupted" else 0


def cmd_focus(args, out):
    ctx = Context(args)
    api = ctx.api()
    if args.clear and args.keywords:
        raise UsageError("Give keywords or --clear, not both.")
    if args.clear or args.keywords:
        words = [] if args.clear else _split_words(args.keywords)
        me = api.set_focus(words)
    else:
        me = api.me()
    focus = me.get("focus") or []
    out.result({"focus": focus}, [f"Focus: {', '.join(focus)}" if focus else "Focus: none (you get any task). Example: spacebio-satellite focus radiation microgravity"])
    return 0


def cmd_outbox(args, out):
    ctx = Context(args)
    outbox = ctx.outbox()
    pending = outbox.pending(ctx.server)
    if args.clear:
        if pending and not args.yes:
            if not sys.stdin.isatty():
                raise UsageError("Add --yes to confirm: the unsent batches would be lost (their tasks will be offered again later).")
            if input(f"Remove {len(pending)} unsent batch(es)? [y/N] ").strip().lower() != "y":
                out.say("Nothing removed.")
                return 0
        removed = outbox.clear(ctx.server)
        out.result({"removed": removed}, [f"Removed {removed} batch(es)."])
        return 0
    if args.flush:
        api = ctx.api()
        runner = Runner(api, outbox, ctx.server, out, sleep=DEPS.sleep, clock=DEPS.clock, now=DEPS.now)
        runner.flush_outbox(RunOptions())
        pending = outbox.pending(ctx.server)
        lines = [f"Accepted {runner.stats.batches} batch(es); {len(pending)} still waiting."]
        out.result({"accepted": runner.stats.batches, "pending": len(pending)}, lines)
        return 0
    rows = [{"task_id": p["task_id"], "source": p["source"], "keyword": p["keyword"], "records": p["records"], "created_at": p["created_at"], "sent": bool(p["batch_id"]),
             "attempts": p["attempts"], "last_error": p["last_error"]} for p in pending]
    lines = [f"{len(rows)} batch(es) waiting to be sent." if rows else "The outbox is empty."]
    lines += [f"  task {r['task_id']} · {r['source']} · {r['keyword']} · {r['records']} records · {'sent, waiting verdict' if r['sent'] else 'not sent'}"
              + (f" · last error: {r['last_error']}" if r["last_error"] else "") for r in rows]
    if rows:
        lines.append("Send them now with: spacebio-satellite outbox --flush")
    out.result({"pending": rows}, lines)
    return 0


def cmd_history(args, out):
    ctx = Context(args)
    rows = ctx.outbox().history(ctx.server, args.n)
    lines = [f"{r['at']}  {r['status']:<9} {r['source']:<9} {r['keyword'][:24]:<24} new {r['new']:<4} completed {r['completed']:<4} +{r['points']}" + (f"  ({r['reason']})" if r["reason"] else "")
             for r in rows] or ["No batches yet."]
    out.result({"history": rows}, lines)
    return 0


def cmd_privacy(args, out):
    out.result({"privacy": privacy_lines()}, privacy_lines())
    return 0


def cmd_version(args, out):
    out.result({"version": __version__, "task_kinds": list(TASK_KINDS), "record_schemas": list(RECORD_SCHEMAS), "python": platform.python_version()}, [f"spacebio-satellite {__version__}"])
    return 0


def cmd_doctor(args, out):
    ctx = Context(args)
    checks = []

    def add(name, state, detail):
        checks.append({"check": name, "state": state, "detail": detail})

    add("python", "ok" if sys.version_info >= (3, 10) else "fail", platform.python_version())
    add("keyring", "ok" if keyring_is_secure() else "warn", keyring_description() + ("" if keyring_is_secure() else " (not secure: login will ask for --allow-plaintext-token)"))
    for label, path in (("config folder", ctx.paths.config_dir), ("data folder", ctx.paths.data_dir)):
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write-test"
            probe.write_text("x")
            probe.unlink()
            add(label, "ok", str(path))
        except OSError as exc:
            add(label, "fail", f"{path}: {exc}")
    api = ctx.api(authed=False)
    meta = None
    try:
        meta = api.meta()
        add("server", "ok", f"{ctx.server} (API {api.api_version or meta.get('api_version')})")
    except (NetworkError, ApiError) as exc:
        add("server", "fail", f"{ctx.server}: {exc}")
    if meta:
        minimum = meta.get("min_client_version") or "0.0.0"
        add("version", "ok" if parse_version(__version__) >= parse_version(minimum) else "fail", f"you have {__version__}, the server needs {minimum} or newer")
        latest = meta.get("latest_client_version")
        if latest and parse_version(latest) > parse_version(__version__):
            add("update", "warn", f"version {latest} is available")
        server_time = parse_iso(meta.get("server_time"))
        if server_time:
            skew = abs((DEPS.now() - server_time).total_seconds())
            add("clock", "ok" if skew < 300 else "warn", f"{int(skew)} s apart from the server" + ("" if skew < 300 else " (fix your clock: leases depend on it)"))
    token, where = ctx.store.get(ctx.server)
    if not token:
        add("login", "warn", "not logged in (spacebio-satellite login)")
    else:
        try:
            me = ApiClient(ctx.server, token, sleep=DEPS.sleep, retries=1).me()
            add("login", "ok", f"{me.get('status')} · token from {where}")
            expires = parse_iso(ctx.settings.device(ctx.server).token_expires_at)
            if expires and (expires - DEPS.now()).days < 14:
                add("token", "warn", f"expires in {max(0, (expires - DEPS.now()).days)} day(s); it renews while you use it")
        except InvalidToken:
            add("login", "fail", "the stored token is not valid any more (spacebio-satellite login)")
        except (NetworkError, ApiError) as exc:
            add("login", "warn", f"could not check the token: {exc}")
    keys = [name for name in ("NCBI_API_KEY", "OPENALEX_API_KEY") if os.environ.get(name)]
    add("your API keys", "ok", ", ".join(keys) + " (used only for requests to that database)" if keys else "none set (optional; OpenAlex asks for a free key)")
    try:
        add("outbox", "ok", f"{len(ctx.outbox().pending(ctx.server))} batch(es) waiting")
    except Exception as exc:                                    # a damaged outbox file deserves a clear message
        add("outbox", "fail", str(exc))
    marks = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL"}
    out.result({"checks": checks}, [f"[{marks[c['state']]}] {c['check']:<14} {c['detail']}" for c in checks])
    return 1 if any(c["state"] == "fail" for c in checks) else 0


COMMANDS = {"login": cmd_login, "logout": cmd_logout, "status": cmd_status, "run": cmd_run, "focus": cmd_focus, "outbox": cmd_outbox, "history": cmd_history,
            "privacy": cmd_privacy, "doctor": cmd_doctor, "version": cmd_version}


# ==================================================================================================== parser
def _common(parser, suppress):
    default = argparse.SUPPRESS if suppress else None
    parser.add_argument("--server", metavar="URL", default=default, help="SpaceBio server (default: the one you logged in to, or the SPACEBIO_SATELLITE_URL variable)")
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS if suppress else False, help="print results and events as JSON lines (for scripts)")
    parser.add_argument("-q", "--quiet", action="store_true", default=argparse.SUPPRESS if suppress else False, help="print only warnings and errors")
    parser.add_argument("-v", "--verbose", action="count", default=argparse.SUPPRESS if suppress else 0, help="print debugging details")


def build_parser():
    parser = argparse.ArgumentParser(prog="spacebio-satellite", description=DESCRIPTION, epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"spacebio-satellite {__version__}")
    _common(parser, suppress=False)
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    def add(name, help_text):
        p = sub.add_parser(name, help=help_text, description=help_text)
        _common(p, suppress=True)
        return p

    p = add("login", "connect this computer to your SpaceBio account (you approve in your browser)")
    p.add_argument("--label", help="a name for this computer, shown in your devices list")
    p.add_argument("--no-browser", action="store_true", help="do not open the browser; just show the address and code")
    p.add_argument("--allow-plaintext-token", action="store_true", help="if no secure keyring exists, keep the token in a private file (less safe)")

    p = add("logout", "disconnect: revoke this device on the server and delete the local token")
    p.add_argument("--local-only", action="store_true", help="only delete the local token; do not contact the server")

    add("status", "your account, points, limits and the local outbox")

    p = add("run", "work: claim tasks, search the databases, send the papers")
    p.add_argument("--for", dest="duration", metavar="TIME", help="work for this long, then stop (30m, 2h, 1h30m). No new task starts after it.")
    p.add_argument("--max-tasks", type=int, metavar="N", help="stop after N tasks")
    p.add_argument("--once", action="store_true", help="ask once for work; stop even if there is nothing to do")
    p.add_argument("--dry-run", action="store_true", help="fetch and clean one task but send nothing and lease nothing")
    p.add_argument("--show-payload", action="store_true", help="with --dry-run, print the exact batch that would be sent")
    p.add_argument("--focus", action="append", metavar="WORDS", help="prefer tasks about these keywords (comma separated; repeatable)")
    p.add_argument("--focus-only", action="store_true", help="take nothing outside --focus")
    p.add_argument("--skip-source", action="append", metavar="SOURCE", help="never take tasks for this database (europepmc, pubmed, crossref, openalex)")
    p.add_argument("--no-wait", action="store_true", help="do not wait for the verdict after sending (collected on the next run)")
    p.add_argument("--max-batch-records", type=int, metavar="N", help="send at most N records per batch (the server's limit still applies)")

    p = add("focus", "show or set the topics you prefer (kept on your account)")
    p.add_argument("keywords", nargs="*", help="keywords to prefer, for example: radiation microgravity")
    p.add_argument("--clear", action="store_true", help="remove your focus")

    p = add("outbox", "batches saved locally and not yet confirmed")
    p.add_argument("--flush", action="store_true", help="send them (or collect their verdict) now")
    p.add_argument("--clear", action="store_true", help="throw them away")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")

    p = add("history", "what the core decided about your recent batches")
    p.add_argument("-n", type=int, default=20, help="how many lines (default 20)")

    add("privacy", "what leaves your computer")
    add("doctor", "check your setup: python, keyring, server, version, login, clock")
    add("version", "show the version")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:                                   # argparse exits by itself for --help, --version and usage errors
        return int(exc.code or 0)
    if not args.command:
        parser.print_help()
        return 2
    out = Output(json_mode=args.json, quiet=args.quiet, verbose=args.verbose)
    try:
        return COMMANDS[args.command](args, out)
    except SatelliteError as exc:
        out.error(str(exc), exc.hint)
        return exc.exit_code
    except KeyboardInterrupt:
        out.error("Interrupted.")
        return 130
    except BrokenPipeError:                                     # `... | head`
        return 0


if __name__ == "__main__":           # pragma: no cover
    sys.exit(main())
