> Mirror of the contract owned by the SpaceBio core (`contrib/SATELLITE_API.md` in the core repository). If the two differ, the core wins: open an issue here.

# SpaceBio Satellite API, contract v1 (implemented in the core: see section 11 for what the implementation adds)

A **satellite** is a small program (`spacebio_satellite`, open source, its own repository) that a community member runs on their own computer. It asks the core for a piece of work, does the heavy part locally (searching public databases), and sends back the result. The core never trusts it.

This document is the contract between the two. The core repository owns it; the satellite repository follows it. Anything not written here is not promised.

## 0. Principles

1. **One door, one brain.** The satellite is a new *transport* for what `contrib` already does (`tasks.claim` to hand out work, `pipeline.process` to clean, deduplicate, verify, score and pay). Same tasks, same records, same ledger, same ranking as the browser. No second set of rules.
2. **The server decides, the client runs.** The core builds the exact request (`tasks.build_request`); the client only executes it against the hosts the task lists. The client never decides relevance, duplicates, points or trust.
3. **A satellite can only add.** It proposes records. It can never delete, merge, edit existing papers, or publish. Anything destructive (relevance cleanup with its 3% cap, dedupe with its 15% cap, the 3D map, social posts) stays in the core.
4. **Limits are data, not constants.** Sizes, delays, quotas and lease times travel in responses, so they change without a new client.
5. **Additive evolution.** Within `v1` the server only adds optional fields; clients ignore fields they do not know. Breaking changes mean `/v2/`.
6. **Nothing personal leaves the machine** except the token and public paper records (see section 9).

## 1. Conventions

* Base URL: `https://<host>/api/satellite/v1/`. HTTPS only. JSON, UTF-8. Request bodies may be gzip (`Content-Encoding: gzip`); the *decompressed* size is limited.
* Every request carries `User-Agent: spacebio-satellite/<version> (<os family>)` and `X-Satellite-Client: <version>`. Nothing else about the machine (no hostname, user name, paths, IP is not stored).
* Authentication: `Authorization: Bearer <token>` on everything except `GET /` and the two `device/` calls. **Cookies and CSRF are not accepted on this API** (a browser session must never reach it).
* Time: ISO 8601 UTC. IDs: integers for tasks, UUID strings for batches.
* Every response carries `X-Satellite-API: 1.<minor>`.

### Errors

Always HTTP status plus one body shape:

```json
{"error": {"code": "task_not_yours_or_expired", "message": "Human readable, English.", "retry_after": 30, "details": {}}}
```

| Status | `code` | Meaning |
|---|---|---|
| 400 | `bad_request` | Not JSON, wrong types |
| 401 | `invalid_token` | Missing, unknown, expired or revoked token |
| 403 | `not_consented` | Consent withdrawn or outdated: reopen the device page |
| 403 | `suspended` | Account suspended (3 strikes); do not retry |
| 403 | `insufficient_scope` | Token lacks the scope |
| 409 | `task_not_yours_or_expired` | Lease expired or task belongs to another device |
| 404 | `not_found` | Unknown batch or task (also for another device's batch) |
| 409 | `duplicate_batch` | Same `client_batch_id` already received; `details` is `{"batch_id": "...", "status": "..."}` of the original |
| 413 | `too_large` | Over the advertised size limit |
| 422 | `invalid_payload` | Structurally valid JSON, unusable content (`details` lists the fields) |
| 426 | `upgrade_required` | Client older than `min_client_version`; `details.latest` says what to install |
| 429 | `rate_limited` | `Retry-After` header and `retry_after` field in seconds |
| 503 | `unavailable` | Try later; the task, if any, was given back without blame |

## 2. Endpoints

| Method and path | Auth | Purpose |
|---|---|---|
| `GET /` | none | API meta: versions, limits, consent version, embedding model |
| `POST /device/code` | none | Start the browser login (device flow) |
| `POST /device/token` | none | Poll until the person approved; returns the token |
| `GET /me` | token | Who am I: status, tier, quotas, points, focus |
| `PATCH /me` | token | Change `focus` only (everything else is done on the website) |
| `POST /tasks/claim` | token | Lease one or more tasks |
| `POST /tasks/{id}/heartbeat` | token | Extend a lease |
| `POST /tasks/{id}/release` | token | Give a task back (no blame) |
| `POST /tasks/{id}/batches` | token | Send the result of a task |
| `GET /batches/{batch_id}` | token | The server's verdict (optionally waits) |
| `POST /device/revoke` | token | This device deletes its own token |

Website pages (not API): `/satellite/` (explanation, consent, my devices, revoke, focus, my contributions), `/satellite/activate/` (where the code is typed).

### 2.1 `GET /` (meta)

```json
{
  "api_version": "1.0",
  "min_client_version": "0.1.0",
  "latest_client_version": "0.1.0",
  "server_time": "2026-11-23T01:00:00Z",
  "consent_version": 1,
  "task_kinds": ["harvest/1"],
  "record_schemas": ["paper/1"],
  "embedding": {"model": "<MODEL_NAME>", "revision": "<pinned>", "dim": 768}
}
```

`embedding` is advertised from v1 so clients can cache it, but no vector task exists until phase 2 (section 7).

### 2.2 Login: device flow (RFC 8628 shape)

The account uses Google sign-in, so there is no password for the client to send. The person approves in the browser instead.

```
POST /device/code
{"client_name": "spacebio-satellite", "client_version": "0.1.0", "device_label": "Home laptop"}
→ 200 {"device_code": "<opaque, 32 bytes>", "user_code": "WDJB-MJHT",
       "verification_uri": "https://<host>/satellite/activate/",
       "verification_uri_complete": "https://<host>/satellite/activate/?code=WDJB-MJHT",
       "expires_in": 600, "interval": 5}
```

The person opens the page (already signed in with Google), sees the consent text (section 9), optionally edits the device name and approves. Then:

```
POST /device/token  {"device_code": "..."}
→ 400 {"error": {"code": "authorization_pending"}}      keep polling every `interval` s
→ 400 {"error": {"code": "slow_down"}}                  add 5 s to the interval
→ 400 {"error": {"code": "access_denied"}}              the person refused
→ 400 {"error": {"code": "expired_token"}}              start again
→ 200 {"access_token": "sbs_...", "token_type": "Bearer", "expires_at": "2027-02-21T00:00:00Z",
       "scopes": ["harvest:run"], "device_id": "uuid"}
```

Token facts:
* Format `sbs_` + 43 base64url characters (256 random bits). The prefix lets secret scanners find leaks.
* The server stores only a hash and the first 8 characters. **Shown once.**
* One token per device; scopes are fixed per token. v1 has one scope: `harvest:run`.
* Lifetime 90 days, **sliding**: each authenticated day extends it (`X-Satellite-Token-Expires` header). Idle devices expire by themselves.
* The client keeps it in the OS keyring. If no secure keyring exists it must say so and refuse to write a plaintext file unless the person passes an explicit flag (and then the file is mode 0600).
* The person can revoke any device on `/satellite/`; an admin can revoke any device or suspend the account on `/manage/automation/`.

### 2.3 `GET /me`

```json
{
  "status": "active",
  "tier": "newcomer",
  "trust": 4,
  "points_total": 120, "points_today": 30, "daily_points": 400, "points_left": 370,
  "tasks_last_hour": 2, "max_tasks_per_hour": 30,
  "focus": ["radiation", "microgravity"],
  "device": {"id": "uuid", "label": "Home laptop", "created_at": "...", "expires_at": "..."},
  "limits": {"claim_min_interval_s": 4, "max_tasks_per_claim": 1, "max_body_bytes": 2000000, "max_records_per_batch": 1000}
}
```

`status` is `active | paused | suspended | consent_needed`. Pausing is done on the website. A paused account gets `200` with `tasks: []` and `reason: "paused"` from `claim`, so a long-running satellite just waits; `403 not_consented` is only for withdrawn or outdated consent.

### 2.4 Claim, heartbeat, release

```
POST /tasks/claim
{"capabilities": ["harvest/1"], "focus": ["radiation"], "focus_only": false,
 "skip_sources": ["openalex"], "max_tasks": 1, "dry_run": false}
```

* `capabilities`: task kinds this client can run. The server never hands out a kind the client did not list. A client that meets an unknown `kind` anyway must release it with `reason: "unsupported_kind"`.
* `focus`: keywords the person prefers (overrides the profile for this call). `focus_only: false` means "prefer, but take anything"; `true` means "nothing else".
* `skip_sources`: sources the client could not reach lately (same idea as the browser's `skip`).
* `dry_run: true` returns what *would* be handed out, **without a lease**, without counting against any limit (`lease_until` is `null`). The client's own dry run then fetches and cleans locally and prints what it would send, but sends nothing: the server has no "dry submit".

```json
{"tasks": [{
   "id": 8123, "kind": "harvest/1", "pipeline": "Papers end to end",
   "lease_until": "2026-11-23T01:15:00Z", "max_lease_until": "2026-11-23T02:00:00Z",
   "source": "europepmc", "keyword": "microgravity", "window": {"start": "2024-01-01", "end": "2024-12-31"},
   "request": {"url": "https://www.ebi.ac.uk/europepmc/...", "pages": 10, "page_size": 100},
   "allowed_hosts": ["www.ebi.ac.uk"], "min_interval_ms": 1200,
   "contact_email": "<the PROJECT's address, never the person's>",
   "record_schema": "paper/1"}],
 "limits": {"max_records_per_batch": 1000}}
```

No task: `{"tasks": [], "reason": "no_tasks | slow_down | daily_limit | hourly_limit | paused", "retry_after": 900}`.

Rules for the client: contact **only** `allowed_hosts`, at most one request per `min_interval_ms` per host, follow pagination as `request` and the source's own cursor say, stop at `pages`.

Heartbeat: `POST /tasks/{id}/heartbeat` extends the lease by the standard lease length up to `max_lease_until`; the answer has the new `lease_until`. Release: `POST /tasks/{id}/release {"reason": "source_unavailable | rate_limited_by_source | user_stopped | unsupported_kind | error", "detail": "optional, 200 chars"}`. Release never counts against the person; an expired lease is the same.

### 2.5 Send a result

```
POST /tasks/8123/batches        Idempotency: the body's client_batch_id
{"client_batch_id": "3f0c...uuid", "started_at": "...", "finished_at": "...", "pages_fetched": 3,
 "error": null,
 "records": [ <paper/1>, ... ]}
→ 202 {"batch_id": "uuid", "status": "received"}
```

* `client_batch_id` is chosen by the client and makes a retry safe: resending the same id returns the original batch (`409 duplicate_batch` carries its `batch_id` and `status` in `details`), never a second copy.
* `error` (instead of records) means "I could not do it": `{"code": "source_unavailable", "message": "..."}`. The task is given back and nobody is blamed (same as the browser).
* The answer is always `202`: the core queues the work (clean, score, deduplicate, verify, save) so the web process is never held. Then:

```
GET /batches/{batch_id}?wait=20       long-poll up to 20 s (max 30)
→ 200 {"batch_id": "uuid", "task_id": 8123, "status": "accepted",
       "counts": {"received": 300, "invalid": 2, "off_topic": 40, "known": 150, "new": 90, "completed": 18,
                  "verified_ok": 10, "verified_bad": 0, "verified_unknown": 1},
       "points": 940, "reason": "", "suspended": false, "limits": {"points_left": 0, "tasks_last_hour": 3}}
```

`status`: `received → processing → accepted | rejected | retry`.
* `rejected`, `reason: "verification_failed"`: a checked record did not match its source. **One forged record rejects the whole batch and counts a strike** (3 strikes suspend). Same rule as the browser.
* `rejected`, `reason: "unconfirmed"`: three or more records checked and none confirmable. No strike.
* `retry`, `reason: "verification_unavailable"`: the core's own checker was down; the task went back to the pool, send nothing again.
* Points are computed by the server only; the client cannot claim any.

### 2.6 Record schema `paper/1`

Identical to what the browser sends today (so `validation.clean_record` is reused unchanged):

```json
{"title": "…", "doi": "10.1234/abc", "pmid": "12345678", "abstract": "…", "authors": "A. One; B. Two",
 "journal": "…", "year": "2024", "language": "eng", "urls": ["https://…"]}
```

Limits: `title` 15 to 500 characters; `abstract` 15,000; `authors` 2,000; `journal` 300; `urls` at most 5 of 400; **a DOI or a PMID is required** (otherwise the record can be neither verified nor merged). The server strips markup and control characters, recomputes `id`, relevance and duplicates itself, and ignores unknown fields.

## 3. Quotas and trust tiers

One ledger and one **daily points cap per person**, shared by browser and every device (so extra computers do not multiply the cap and the ranking stays fair). Values below are the current `contrib` numbers; the `veteran` tier is a first guess and is configuration (see section 10).

| Tier | Trust | Verification sample of new records | Tasks per hour |
|---|---|---|---|
| newcomer | < 20 | min(new, 10) | 30 (as today) |
| trusted | 20 to 199 | 25% (at least 2) | 30 |
| veteran (configurable) | ≥ 200 | 10% (at least 2) | 60 |

Trust grows with verified records and falls by 10 per strike. Suspension is by account, not by device.

## 4. What the server stores

In `contrib/models.py`: `SatelliteDevice` (user, label, **SHA-256 of the token** and its first 8 characters, scopes, program version, operating-system family, created, last seen, expires, revoked and by whom), `DeviceAuthRequest` (hashed device code, user code, status, interval, expiry), `SatelliteBatch` (device, task, `client_batch_id` unique per device, status, reason, sizes, the records **until processed**, the verdict counts), `SatelliteControl` (the administrators' switches, one row). `ContributorProfile` gets `satellite_consent_at`, `satellite_consent_version`, `satellite_active` and `satellite_focus`; `HarvestTask` gets `assigned_device`; `Submission` gets `device` and `known`. No IP address is stored, in line with the browser module (the address of a caller is only ever the key of a counter that expires within the hour, as a keyed hash).

## 5. Governance (`/manage/automation/`)

A Satellites tab for administrators (`/manage/automation/satellites/`, linked from the Automation panel): devices (last seen, client version, status), batches accepted and rejected, trust and strikes, **revoke a device**, **suspend or reinstate an account**, a global switch "accept satellite batches" (when off, `claim` answers `paused` and `batches` answers `503 unavailable`) and the minimum client version. Every admin action is written to `entities.AuditEntry` (`satellite.*` actions).

## 6. Security checklist (for the implementation)

* Tokens hashed at rest; constant-time comparison; revoked or expired tokens answer `401` identically to unknown ones.
* Device codes single use, 10-minute expiry, user codes rate limited, approval requires an authenticated, consented session.
* `claim`, `batches` and `device/*` rate limited per token or per IP (device endpoints, which have no token).
* Decompressed body cap (zip-bomb guard); JSON depth and record count limits before any parsing work.
* The API ignores cookies; no CORS headers.
* No endpoint takes a URL, path or command from the client.
* Everything a batch changes goes through the same `store.save_record` the browser path uses.

## 7. Reserved for later phases (not part of v1 work)

* `complete/1` task: fill missing abstract, authors or identifiers of given papers (feeds "Complete paper records").
* `embed/1` task and `vector/1` record `{"id", "model", "revision", "dim", "encoding": "f16le-base64", "values"}`. Accepted **only** from `trusted` or higher, only when `model`, `revision` and `dim` match the advertised `embedding`, and a sample is recomputed by the core and compared by cosine similarity (threshold to be measured on real data; a miss is a strike).
* `mirror/1` tasks for the research data mirrors (OSDR, GEO, …), sharded by accession.
* `webhook`/push notifications: not planned; clients poll.

Never on a satellite: `broadcast`, `broadcast-autopilot`, `broadcast-publish`, `relevance` cleanup, `dedupe`, `galaxy`.

## 8. Compatibility and deprecation

* `X-Satellite-API: 1.<minor>` grows with additive changes. A client that sees a higher minor keeps working.
* Raising `min_client_version` makes older clients get `426 upgrade_required` on their next call; announce it in `latest_client_version` first.
* A removed behaviour is announced with the `Sunset` header at least 90 days ahead.

## 9. What leaves the computer (the consent text must say exactly this)

Stays on the computer: the access token (system keyring or a private file). The program works until the person stops it (Ctrl-C) or reaches the limit given (`--for`, `--max-tasks`); the core never starts it.

Sent to SpaceBio: the access token, public paper records found in public databases (title, abstract, authors, journal, year, language, DOI or PubMed ID, full-text links), the client name and version, the operating system family, and the device label the person typed. **Not** sent: file names, other programs, API keys the person may use for sources, personal e-mail (the sources only ever see the **project's** contact address from the task).

Kept by SpaceBio: per device, its label, the program version, the system family, the last use and a one-way fingerprint (SHA-256) of the token. A batch is deleted once checked; only the verdict counts stay. Papers and points are linked to the account.

IP address: not kept with the account or the contributions. The servers and the protection layer in front of them see it while answering, and the abuse counters hold it for at most an hour (the satellite's own counters use a keyed hash; the shield's use the address itself, with a short expiry).

The public databases (Europe PMC, PubMed, Crossref, OpenAlex) **do** see the person's IP address when the satellite contacts them directly, exactly as for the browser module. Three strikes switch contributions off for the whole account, browser included. Contributions are opt-in per account, separate from the browser consent (`satellite_consent_version`), withdrawable at any time, and "Delete my contribution data" removes profile, ledger, devices and submissions (papers stay: they are public records).

## 10. Decisions taken

1. **One shared pool of tasks** with the browser module (`HarvestTask`), no partition. A lease already prevents double work. Revisit only if browsers and satellites are seen competing for the same windows.
2. **Batch size for satellites: up to 1,000 records** (a task may ask for `pages` up to 10). The browser stays at 300. The value travels in `limits.max_records_per_batch`, so it can change without a new client.
3. **Token lifetime: 90 days, sliding.** Configurable by the server (`SATELLITE_TOKEN_DAYS`).
4. **Separate consent** from the browser module (`satellite_consent_version`): a program that runs unattended and keeps a token on disk is a different thing from an open tab.
5. **Tier numbers are settings, not constants.** The `veteran` tier (trust >= 200, 10% sample, 60 tasks per hour) is a first guess, set in configuration and tuned with real data. The `newcomer` and `trusted` values are today's `contrib` numbers.

Status: contract v1 is implemented in the core (`contrib/satellite/`) and was exercised end to end by the real program (see `contrib/tests_satellite_live.py`). **Wording of section 9 (the consent text, shown on `/satellite/` and `/satellite/activate/`) still needs the owner's review before it goes live.**

Reference client: `spacebio_satellite` (a separate repository) implements this contract for `harvest/1`. Its own tests run against a stand-in core (`devtools/mock_core.py`) and the live public databases; the core's tests run the real program against the real core.

## 11. What the implementation does exactly (additions and choices the draft left open)

Everything here is additive: a client written to sections 1 to 10 keeps working.

* **Extra fields.** `GET /` and `GET /me` carry `accepting_batches` (the administrators' switch). `claim` limits also carry `points_left` and `tasks_last_hour`. `heartbeat` answers `{task_id, lease_until, max_lease_until}`, `release` answers `{released, task_id}`, `device/revoke` answers `{revoked: true}`.
* **A program that could not do the task** sends `error` without records. The answer is `202`, and the batch is at once `retry` with `reason: "given_back"`; the task is back in the pool and nobody is blamed.
* **Order of checks** on an authenticated call: program version (`426`), token (`401`), per-device rate (`429`), scope (`403`), account state (`403 suspended | not_consented`), then the body. `/me` and `device/revoke` work in every account state, and `device/revoke` and `GET /` skip the version check, so an old or suspended program can still see its state and log out. A paused account may still *send* a batch it had finished; it just gets no new work.
* **Limits** (all settings, defaults in `contrib/satellite/config.py`): 120 requests a minute per device; 10 login codes an hour and 30 token polls a minute per network address; one `claim` every 4 seconds per device; 10 devices per person; a body of at most 2,000,000 bytes **decompressed** (a gzip bomb is stopped while it inflates); at most 1,000 records; a task may ask for up to 10 pages; a lease is 15 minutes, extendable to 60 by heartbeats.
* **Release.** `user_stopped`, `rate_limited_by_source` and `unsupported_kind` do not use up one of the task's three attempts; `source_unavailable` and `error` do (after the third the task stops being offered).
* **Long-poll.** `GET /batches/{id}?wait=N` holds a web thread, so at most `SATELLITE_MAX_WAITERS` (3) are held at once across the site; above that the answer is immediate and the program asks again (it already loops).
* **Processing** is the job `satellite-batches` (command `process_satellite_batches`), every minute, in its own scheduler lane (`intake`) so a long news job cannot delay verdicts. A crashed batch is retried up to 3 times, then rejected as `processing_error` (our failure, no strike). A task whose result is waiting is never recycled when its lease runs out.
* **Reasons of a rejected batch:** `verification_failed` (strike), `unconfirmed`, `suspended`, `consent_withdrawn`, `task_not_held`, `task_gone`, `processing_error` (the last five without a strike).
* **The veteran tier applies to satellites only** (`SATELLITE_VETERAN_TRUST` 200, `SATELLITE_VETERAN_SAMPLE` 0.1, `SATELLITE_VETERAN_TASKS_PER_HOUR` 60). The browser keeps today's numbers whatever the person's trust. Trust, strikes, points, the daily cap and suspension are one per person across both.
* **Retention.** The records of a batch are removed from the database as soon as it is settled; verdict rows are kept 90 days; login attempts 2 days.
* **Login page.** `/satellite/activate/` shows what is asking (computer name, operating-system family, program version, age) and warns to approve only a login the person started. Approving is where the satellite consent is given (a person who already agreed is not asked again). Withdrawing consent, or "Delete my contribution data" on either page, disconnects every computer at once.
* **Where it is configured:** *Environment keys* (group "Satellites"): install command and source address shown to members, token lifetime and the veteran numbers. *Satellites tab*: the global switch and the minimum and latest program versions.
