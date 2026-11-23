# Architecture

The program is deliberately small: no Django, no web server, no database server. Everything the core needs to trust is decided **on the core's side**; this client only does the work that is heavy for a server (searching the public databases) and hands the result over.

```
                       +----------------------+
  public databases <-- |  fetcher (guarded)   |  only the hosts the task lists, https, paced, size-capped
  (Europe PMC, ...)    +----------+-----------+
                                  | raw records
                       +----------v-----------+
                       |  sources/*  readers  |  JSON/XML -> raw paper dicts, page by page
                       +----------+-----------+
                                  |
                       +----------v-----------+      +---------------------+
                       |  cleaning (paper/1)  |      |  outbox (SQLite)    |  batch saved BEFORE it is sent,
                       +----------+-----------+      |  + history          |  removed only after the verdict
                                  |                  +----------^----------+
                       +----------v-----------+                 |
   SpaceBio core  <--> |  runner (the loop)   |-----------------+
   /api/satellite/v1/  |  api   (endpoints)   |
                       |  auth  (device flow) |
                       +----------+-----------+
                                  |
                       +----------v-----------+
                       |  cli / output        |  commands, exit codes, --json
                       +----------------------+
```

## The work loop (`runner.py`)

1. `GET /me`: refuse early when the account is suspended or needs consent.
2. Finish what an earlier run left in the outbox (`flush_outbox`).
3. `POST /tasks/claim`, no faster than the server's `claim_min_interval_s`.
4. For each task: pick the reader (`sources/`), run it with a `tick` callback that checks for Ctrl-C and extends the lease (`heartbeat`) when it runs short.
5. Clean the records (`cleaning.py`), cap them at the server's `max_records_per_batch`.
6. Save the batch in the outbox, `POST /tasks/{id}/batches` (idempotent by `client_batch_id`), then poll `GET /batches/{id}?wait=20` for the verdict.
7. Remove it from the outbox, record the verdict in the history, update the points.

Failure handling, all covered by tests:

| Situation | What happens |
|---|---|
| A database answers 429/5xx | Task released (`rate_limited_by_source` / `source_unavailable`); that database is skipped for 6 hours |
| A database wants an API key (401/403) | Same, with a message naming the variable; OpenAlex keeps its first page if only paging was refused |
| The task asks for a host it did not list | Never contacted; task released with `error` |
| Unknown task kind or database | Released as `unsupported_kind` |
| Lease expired | `409`: batch dropped, nobody blamed |
| Network or server down while sending | Batch stays in the outbox; next run resends with the same id |
| Crash between sending and the verdict | Next run's resend gets `duplicate_batch` with the original id and collects the verdict |
| Verdict `rejected` | Reported, counted, removed; `suspended: true` stops the run (exit 4) |
| Ctrl-C | Task in hand released (`user_stopped`), outbox kept, exit 130 |

## Adding a database

1. Create `sources/<name>.py` with a pure `parse_page(data) -> (records, next)` and a `Source` subclass whose `collect` follows the pages (copy `crossref.py`, the shortest).
2. Register it in `sources/__init__.py`.
3. Add its host to `fetcher.KEY_ENV` if it takes a personal API key.
4. Record a real answer into `tests/fixtures/`, add a parser test (it must produce records that survive `cleaning.clean_record`) and a pagination test using `devtools/mock_core.py`.

The *server* decides which sources exist in the pool of tasks; a reader here without a task is harmless, and a task for a missing reader is given back untouched.

## Adding a task kind

Only after the kind is in the contract. Add it to `TASK_KINDS` in `__init__.py` (it is announced to the core as a capability) and give `runner._process` a branch for it. Until then the client refuses anything else with `unsupported_kind`.

## Why these choices

* **requests, keyring, platformdirs, defusedxml** are the only dependencies. `defusedxml` because PubMed answers XML and a hostile or broken answer must never be expanded.
* **argparse**, not a framework: the command set is small and the help text is the documentation.
* **SQLite from the standard library** for the outbox: durable, no server, atomic.
* **No threads**: one task at a time; politeness toward the databases matters more than speed.
* **The token is never stored in a plain settings file** and never printed (a test checks every command's output for it).
