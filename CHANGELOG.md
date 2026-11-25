# Changelog

## 0.2.0 (2026-11-24)

Still contract v1 (`harvest/1`, `paper/1`); works with servers that do not ask for approval, exactly as before.

* **`contributions`: what you added.** Lists the papers your work put into SpaceBio, newest first, with title, journal, link and points; `--new` / `--completed` filter, `--limit` / `--page` page through (`--json` for scripts). The same list is on the website, at "What you added" (`/contribute/mine/`), with a CSV download.
* **Approval by an administrator.** When the server requires it, a freshly connected computer waits until an administrator approves it. `login` and `status` say so (and `--json` carries `approval`: `pending` or `approved`), and `run` stops with exit code 4 and a clear hint instead of failing obscurely.
* **No more piling up on a server that is not checking batches.** If the server has not given a verdict for 3 batches in a row, `run` stops with a message saying the problem is on the server's side and that nothing is lost: the batches stay in the outbox and their verdicts are collected on the next run. Before, the program kept harvesting and sending for hours.
* **Searches are exact phrases.** The keyword is searched in quotes, so a task brings the papers that contain it and not whatever shares a few of its words (Europe PMC, PubMed and OpenAlex; the server builds those URLs). Crossref cannot search a phrase, so the program checks it itself: only papers whose title or abstract contain the keyword (whole words, any case) are sent, the line of the run says how many were left out, and it gives up after 3 pages in a row without a match. Before, a Crossref task sent ~1000 papers of which almost all were thrown away by the server.
* **A task is never done twice.** If the server offers a task whose batch was already sent and is still being checked, it is skipped instead of harvested and sent again.
* **A clearer error when the server is the wrong one.** "did not answer with the documented JSON" now names the server, the HTTP status and the content type, and suggests `--server` / `SPACEBIO_SATELLITE_URL`.
* The "still being checked" line now says what it means: the server did not give a verdict within 3 minutes, the batch is safe in the outbox.
* `devtools/mock_core.py`: `--approval-seconds` simulates an administrator who approves after a while.

## 0.1.0 (2026-11-23)

First version. Implements contract v1 (`harvest/1`, `paper/1`).

* Commands: `login` (device flow), `logout`, `status`, `run`, `focus`, `outbox`, `history`, `privacy`, `doctor`, `version`; `--json`, `--quiet`, `--verbose`, `--server`.
* Readers for Europe PMC, PubMed (esearch + efetch), Crossref and OpenAlex, tested against real recorded answers and checked live against the public APIs.
* Token in the OS keyring (explicit opt-in for a private file); settings without secrets.
* Local outbox: batches saved before sending, idempotent resend, verdict collection, history.
* Guarded fetcher: allowed hosts only, https, pacing, redirect and size limits, own API keys per host.
* Graceful handling of busy databases, databases that need an API key (OpenAlex paging), expired leases, a down server, suspension, Ctrl-C.
* `devtools/mock_core.py`: a stand-in core and databases for tests and offline demos.
