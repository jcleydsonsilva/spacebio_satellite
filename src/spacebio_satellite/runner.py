"""The work loop: claim a task, run it against the public database, clean the records, send one batch, read the verdict.

Safety nets, all from the contract:
* a batch is saved in the local outbox before it is sent and removed only after the verdict, so a crash loses nothing;
* resending is harmless (``client_batch_id``); a task whose lease expired is dropped without blame;
* Ctrl-C finishes cleanly: the task in hand is given back, pending batches stay in the outbox;
* a database that says "busy" gives the task back and is left alone for six hours;
* the program only ever adds records: it has no way to delete, merge, edit or publish anything.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field

from . import TASK_KINDS
from .cleaning import clean_records
from .errors import (ApiError, DuplicateBatch, HostNotAllowed, InvalidPayload, NetworkError, NotConsented, PendingApproval, RateLimited, SourceError, StopRequested, Suspended,
                     TaskExpired, TooLarge, Unavailable)
from .fetcher import SourceHttp
from .outbox import Outbox
from .sources import get_source
from .util import iso, parse_iso, utcnow

FINAL = ("accepted", "rejected", "retry")
SKIP_SOURCE_SECONDS = 6 * 3600
VERDICT_TIMEOUT = 180
DEFAULT_MAX_RECORDS = 1000
RELEASE_REASONS = {"source_unavailable", "rate_limited_by_source", "user_stopped", "unsupported_kind", "error"}
EXPLAIN = {
    "verification_failed": "a record did not match its source (this counts as a strike: please report a bug if you did not change anything)",
    "unconfirmed": "none of the checked records could be confirmed at the source",
    "verification_unavailable": "the core could not verify right now; the task went back to the pool",
}


@dataclass
class RunOptions:
    duration: float | None = None          # seconds; no new task is started after it
    max_tasks: int | None = None
    once: bool = False                     # one claim, then stop even if nothing was offered
    dry_run: bool = False                  # fetch and clean, show what would be sent, send nothing
    show_payload: bool = False
    focus: list | None = None
    focus_only: bool = False
    skip_sources: list = field(default_factory=list)
    wait_verdict: bool = True
    max_batch_records: int | None = None


@dataclass
class RunStats:
    tasks: int = 0
    batches: int = 0
    records_sent: int = 0
    new: int = 0
    completed: int = 0
    points: int = 0
    rejected: int = 0
    retried: int = 0
    released: int = 0
    expired: int = 0
    stopped_reason: str = ""

    def as_dict(self):
        return dict(vars(self))


class LeaseKeeper:
    """Extends the lease while a long task is still running."""

    def __init__(self, api, task, now, *, enabled=True, margin=180):
        self.api, self.task_id, self.now, self.enabled, self.margin = api, task.get("id"), now, enabled, margin
        self.lease_until = parse_iso(task.get("lease_until"))

    def tick(self):
        if not self.enabled or self.lease_until is None:
            return
        if (self.lease_until - self.now()).total_seconds() >= self.margin:
            return
        try:
            answer = self.api.heartbeat(self.task_id)
            self.lease_until = parse_iso(answer.get("lease_until")) or self.lease_until
        except (NetworkError, Unavailable, RateLimited):
            pass                                                # try again at the next page; a lost lease is reported by the submit


class Runner:
    def __init__(self, api, outbox: Outbox, server, out, *, sleep=time.sleep, clock=time.monotonic, now=utcnow, http_factory=None, stop=None):
        self.api, self.outbox, self.server, self.out = api, outbox, server, out
        self.sleep, self.clock, self.now = sleep, clock, now
        self.stop = stop or (lambda: False)
        self.http_factory = http_factory or (lambda task: SourceHttp(task.get("allowed_hosts") or [], min_interval_ms=task.get("min_interval_ms", 1200)))
        self.skip_until: dict[str, float] = {}
        self.limits: dict = {}
        self.last_claim: float | None = None
        self.stats = RunStats()

    # ================================================================================================ the loop
    def run(self, options: RunOptions) -> RunStats:
        stats = self.stats = RunStats()
        deadline = self.clock() + options.duration if options.duration else None
        me = self.api.me()
        self._check_account(me)
        self.limits = dict(me.get("limits") or {})
        self.out.stamp(f"connected as {me.get('tier', 'contributor')} · {me.get('points_today', 0)} points today · {me.get('points_left', '?')} left")
        try:
            if not options.dry_run:
                self.flush_outbox(options)
            done = 0
            while True:
                if self.stop():
                    stats.stopped_reason = "interrupted"
                    break
                if options.max_tasks is not None and done >= options.max_tasks:
                    stats.stopped_reason = "max_tasks"
                    break
                if deadline is not None and self.clock() >= deadline:
                    stats.stopped_reason = "time_up"
                    break
                claimed = self._claim(options)
                tasks = claimed.get("tasks") or []
                if not tasks:
                    reason = claimed.get("reason") or "no_tasks"
                    self.out.stamp(self._why_no_task(reason))
                    if reason == "daily_limit":
                        stats.stopped_reason = "daily_limit"
                        break
                    if options.once or options.dry_run:
                        stats.stopped_reason = reason
                        break
                    wait = min(max(int(claimed.get("retry_after") or 60), 5), 600)
                    if deadline is not None and self.clock() + wait >= deadline:
                        stats.stopped_reason = "time_up"
                        break
                    self._nap(wait)
                    continue
                for task in tasks:
                    self._process(task, options)
                    done += 1
                    if self.stop():
                        break
                if options.once:
                    stats.stopped_reason = "once"
                    break
        except StopRequested:
            stats.stopped_reason = "interrupted"
        return stats

    # ================================================================================================ account and claiming
    def _check_account(self, me):
        status = me.get("status", "active")
        if status == "suspended":
            raise Suspended("suspended", "This account is suspended from contributing.")
        if status == "consent_needed":
            raise NotConsented("not_consented", "Your consent is missing or outdated.")
        if (me.get("device") or {}).get("approval") == "pending":
            raise PendingApproval("pending_approval", "This computer is waiting for approval by a SpaceBio administrator.")

    def _active_skips(self, options):
        now = self.clock()
        self.skip_until = {s: t for s, t in self.skip_until.items() if t > now}
        return sorted(set(options.skip_sources) | set(self.skip_until))

    def _claim(self, options):
        gap = float(self.limits.get("claim_min_interval_s", 4) or 0)
        if self.last_claim is not None and gap:
            wait = gap - (self.clock() - self.last_claim)
            if wait > 0:
                self._nap(wait)
        self.last_claim = self.clock()
        try:
            answer = self.api.claim(capabilities=TASK_KINDS, focus=options.focus, focus_only=options.focus_only, skip_sources=self._active_skips(options),
                                    max_tasks=1, dry_run=options.dry_run)
        except RateLimited as exc:
            return {"tasks": [], "reason": "slow_down", "retry_after": exc.retry_after or 30}
        self.limits.update(answer.get("limits") or {})
        return answer

    @staticmethod
    def _why_no_task(reason):
        return {
            "no_tasks": "nothing to do right now", "slow_down": "asked to slow down", "hourly_limit": "hourly limit reached, resting",
            "daily_limit": "daily goal reached, thank you!", "paused": "contributions are paused on the website",
        }.get(reason, f"no task offered ({reason})")

    def _nap(self, seconds):
        """Sleep in short slices so Ctrl-C and the time limit are noticed quickly."""
        end = self.clock() + seconds
        while True:
            remaining = end - self.clock()
            if remaining <= 0 or self.stop():
                return
            self.sleep(min(1.0, remaining))

    # ================================================================================================ one task
    def _process(self, task, options):
        stats = self.stats
        stats.tasks += 1
        tid, source_name, keyword = task.get("id"), str(task.get("source") or ""), str(task.get("keyword") or "")
        window = task.get("window") or {}
        label = f"task {tid} · {source_name} · {keyword} · {window.get('start', '?')[:4]}"
        if task.get("kind") not in TASK_KINDS:
            return self._release(task, "unsupported_kind", f"kind {task.get('kind')}", options)
        source = get_source(source_name)
        if source is None:
            return self._release(task, "unsupported_kind", f"source {source_name}", options)
        self.out.stamp(label)
        keeper = LeaseKeeper(self.api, task, self.now, enabled=not options.dry_run)
        pages = [0]
        started = self.now()

        def tick(page):
            if self.stop():
                raise StopRequested("stop requested")
            pages[0] = page + 1
            keeper.tick()

        cap = min(int(self.limits.get("max_records_per_batch") or DEFAULT_MAX_RECORDS), options.max_batch_records or 10**9)
        http = self.http_factory(task)
        notes: list = []
        try:
            raw = source.collect(task, http, max_records=cap, tick=tick, notes=notes)
        except StopRequested:
            self._release(task, "user_stopped", "", options)
            raise
        except HostNotAllowed as exc:
            self.out.warn(f"{label}: the task asked for a host it did not allow ({exc}); ignoring it")
            return self._release(task, "error", "host_not_allowed", options)
        except SourceError as exc:
            reason = exc.code if exc.code in RELEASE_REASONS else ("error" if exc.code == "source_bad_answer" else "source_unavailable")
            if exc.busy:
                self.skip_until[source_name] = self.clock() + SKIP_SOURCE_SECONDS
                self.out.warn(f"{source_name} is busy; leaving it alone for 6 hours")
            elif exc.code == "source_needs_key":
                self.skip_until[source_name] = self.clock() + SKIP_SOURCE_SECONDS
                self.out.warn(f"{label}: {exc}. Leaving {source_name} alone for 6 hours.")
            else:
                self.out.warn(f"{label}: {exc}")
            return self._release(task, reason, str(exc), options)
        except TaskExpired:
            stats.expired += 1
            self.out.warn(f"{label}: the lease expired before the work was done; the task went back to the pool")
            return None
        for note in notes:
            self.out.warn(f"{label}: {note}")
        records, cstats = clean_records(raw, max_records=cap)
        batch = {"client_batch_id": str(uuid.uuid4()), "started_at": iso(started), "finished_at": iso(self.now()), "pages_fetched": pages[0], "error": None, "records": records}
        self.out.stamp(f"   {pages[0]} page(s), {cstats['received']} found, {len(records)} to send ({cstats['invalid']} unusable, {cstats['duplicates']} repeated)")
        if options.dry_run:
            return self._show_dry_run(task, batch, cstats, options)
        self.outbox.add(self.server, tid, batch, source_name, keyword)
        entry = {"client_batch_id": batch["client_batch_id"], "task_id": tid, "batch": batch, "source": source_name, "keyword": keyword, "batch_id": ""}
        return self._deliver(entry, options)

    def _show_dry_run(self, task, batch, cstats, options):
        self.out.say("   DRY RUN: nothing was sent and no task was leased.")
        for record in batch["records"][:3]:
            self.out.say(f"   - {record['title'][:90]}  [{record['doi'] or 'pmid:' + record['pmid']}]")
        if len(batch["records"]) > 3:
            self.out.say(f"   ... and {len(batch['records']) - 3} more")
        if options.show_payload and not self.out.json_mode:
            self.out.say(json.dumps(batch, indent=2, ensure_ascii=False))
        self.out.event("dry_run", task_id=task.get("id"), records=len(batch["records"]), stats=cstats, **({"batch": batch} if options.show_payload else {}))

    def _release(self, task, reason, detail, options):
        if options.dry_run:
            return None
        self.stats.released += 1
        try:
            self.api.release(task.get("id"), reason, detail)
            self.out.stamp(f"   gave task {task.get('id')} back ({reason})")
        except (ApiError, NetworkError) as exc:
            self.out.warn(f"could not give task {task.get('id')} back ({exc}); its lease will expire by itself")
        return None

    # ================================================================================================ sending and the verdict
    def flush_outbox(self, options: RunOptions):
        pending = self.outbox.pending(self.server)
        if not pending:
            return
        self.out.stamp(f"{len(pending)} batch(es) from an earlier run are waiting; finishing them first")
        for entry in pending:
            if self.stop():
                break
            self._deliver(entry, options)

    def _deliver(self, entry, options):
        cbid = entry["client_batch_id"]
        batch_id = entry.get("batch_id") or ""
        if not batch_id:
            try:
                answer = self.api.submit_batch(entry["task_id"], entry["batch"])
                batch_id = str(answer.get("batch_id") or "")
                self.outbox.mark_sent(cbid, batch_id)
            except DuplicateBatch as exc:
                batch_id = str((exc.details or {}).get("batch_id") or "")
                if not batch_id:
                    self.outbox.remove(cbid)
                    return None
                self.outbox.mark_sent(cbid, batch_id)
            except TaskExpired:
                self.stats.expired += 1
                self._finish_entry(entry, "expired", "task_not_yours_or_expired")
                self.out.warn(f"task {entry['task_id']}: the lease expired before the batch arrived; nothing was stored and nobody is blamed")
                return None
            except (InvalidPayload, TooLarge) as exc:
                self._finish_entry(entry, "rejected", exc.code)
                self.out.warn(f"task {entry['task_id']}: the core refused the batch format ({exc}); it was dropped")
                return None
            except (NetworkError, Unavailable, RateLimited) as exc:
                self.outbox.mark_failed(cbid, exc)
                self.out.warn(f"could not send the batch of task {entry['task_id']} ({exc}); it stays in the outbox for the next run")
                return None
        if not options.wait_verdict:
            self.out.stamp(f"   sent (batch {batch_id[:8]}); the verdict will be collected on the next run")
            return None
        verdict = self._await_verdict(batch_id)
        if verdict is None:
            self.out.stamp(f"   batch {batch_id[:8]} is still being checked; it stays in the outbox")
            return None
        self.outbox.remove(cbid)                                   # the verdict is final: never keep it, even if it ends in a suspension
        self._apply_verdict(entry, batch_id, verdict)
        return verdict

    def _await_verdict(self, batch_id):
        end = self.clock() + VERDICT_TIMEOUT
        while True:
            try:
                answer = self.api.get_batch(batch_id, wait=20)
            except ApiError as exc:
                if exc.status == 404:
                    return {"status": "rejected", "reason": "lost_by_server", "counts": {}, "points": 0}
                raise
            except (NetworkError, Unavailable, RateLimited):
                return None
            if answer.get("status") in FINAL:
                return answer
            if self.clock() >= end or self.stop():
                return None
            self.sleep(1)

    def _finish_entry(self, entry, status, reason):
        self.outbox.record(self.server, task_id=entry["task_id"], source=entry.get("source", ""), keyword=entry.get("keyword", ""), status=status, reason=reason,
                           received=len(entry["batch"].get("records") or []))
        self.outbox.remove(entry["client_batch_id"])

    def _apply_verdict(self, entry, batch_id, verdict):
        stats = self.stats
        status, reason = verdict.get("status"), str(verdict.get("reason") or "")
        counts = verdict.get("counts") or {}
        points = int(verdict.get("points") or 0)
        new, completed = int(counts.get("new") or 0), int(counts.get("completed") or 0)
        received = len(entry["batch"].get("records") or [])
        self.outbox.record(self.server, task_id=entry["task_id"], source=entry.get("source", ""), keyword=entry.get("keyword", ""), batch_id=batch_id, status=status,
                           reason=reason, received=received, new=new, completed=completed, points=points)
        if status == "accepted":
            stats.batches += 1
            stats.records_sent += received
            stats.new += new
            stats.completed += completed
            stats.points += points
            self.out.stamp(f"   accepted: {new} new, {completed} completed, +{points} points")
        elif status == "rejected":
            stats.rejected += 1
            self.out.warn(f"task {entry['task_id']} rejected: {EXPLAIN.get(reason, reason or 'no reason given')}")
        else:
            stats.retried += 1
            self.out.stamp(f"   {EXPLAIN.get(reason, 'the core asked to retry later')}")
        self.out.event("batch", task_id=entry["task_id"], batch_id=batch_id, status=status, reason=reason, counts=counts, points=points)
        if verdict.get("suspended"):
            raise Suspended("suspended", "This account was suspended after repeated rejected batches.")
