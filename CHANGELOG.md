# Changelog

## 0.1.0 (2026-11-23)

First version. Implements contract v1 (`harvest/1`, `paper/1`).

* Commands: `login` (device flow), `logout`, `status`, `run`, `focus`, `outbox`, `history`, `privacy`, `doctor`, `version`; `--json`, `--quiet`, `--verbose`, `--server`.
* Readers for Europe PMC, PubMed (esearch + efetch), Crossref and OpenAlex, tested against real recorded answers and checked live against the public APIs.
* Token in the OS keyring (explicit opt-in for a private file); settings without secrets.
* Local outbox: batches saved before sending, idempotent resend, verdict collection, history.
* Guarded fetcher: allowed hosts only, https, pacing, redirect and size limits, own API keys per host.
* Graceful handling of busy databases, databases that need an API key (OpenAlex paging), expired leases, a down server, suspension, Ctrl-C.
* `devtools/mock_core.py`: a stand-in core and databases for tests and offline demos.
