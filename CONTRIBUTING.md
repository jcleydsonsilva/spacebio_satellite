# Contributing

Thank you for helping. This is a small, deliberately boring program: please keep it that way.

## Set up and test

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest                    # no network; never touches your real keyring or settings
```

Tests run with an in-memory keyring, a private `SPACEBIO_SATELLITE_HOME` and a fake clock (see `tests/conftest.py`), against `devtools/mock_core.py`. If your change needs the contract to change, change the core's `contrib/SATELLITE_API.md` first (the core owns it) and mirror it into `docs/CONTRACT.md`.

## Rules of the house

1. **It only adds.** No feature may delete, edit, merge or publish anything on SpaceBio's side.
2. **Nothing personal leaves the computer.** If you add a field to a request, update `privacy.py`, `docs/CONTRACT.md` and the privacy test.
3. **The token is never printed or stored in plain settings.** `tests/test_cli.py::test_the_token_never_appears_in_any_output` must stay green.
4. **Only listed hosts.** All traffic to public databases goes through `fetcher.SourceHttp`.
5. **Parsers are tested against real recorded answers** in `tests/fixtures/`, not invented ones.
6. **Add dependencies only with a strong reason.** Four is already the budget.
7. **Failures are exceptions with an exit code** (`errors.py`) and a hint for the person; no bare tracebacks reach the user.

## Style

`ruff check src devtools tests` should be clean. Comments explain *why*. User-facing messages are plain English, short, and say what to do next.
