# SpaceBio Satellite

A small command-line program that lets you **lend a little of your computer to SpaceBio**.

It asks the SpaceBio core for a small task ("search *microgravity* in Europe PMC for 2023"), runs that search against the public scientific databases **from your computer**, cleans the papers it finds and sends them back. The core then checks a sample of every batch against the original source before it accepts anything. You earn points and a place in the (opt-in) community ranking.

> **Status: alpha (0.1.0).** The program is complete and tested against a faithful stand-in of the SpaceBio core ([`devtools/mock_core.py`](devtools/mock_core.py)) and against the real Europe PMC, PubMed, Crossref and OpenAlex APIs. The server side of the contract ([`docs/CONTRACT.md`](docs/CONTRACT.md)) is being built in the SpaceBio core, so you cannot log in to the production site yet.

## What it does, and what it never does

| It does | It never does |
|---|---|
| Fetch papers (title, abstract, authors, journal, year, identifiers) from Europe PMC, PubMed, Crossref and OpenAlex | Delete, merge or edit anything in SpaceBio: it can only **add** records |
| Talk **only** to the hosts the core lists for each task, over https, at a polite pace | Post anything anywhere, or run any other pipeline of SpaceBio |
| Keep your access token in the operating system's keyring | Send your IP, user name, host name, files, other programs or your own API keys to SpaceBio |
| Save every batch locally before sending it, so a crash loses nothing | Run in the background by itself: it works only while you run it |
| Show exactly what would be sent (`--dry-run`) | Follow a redirect to a host the task did not allow |

Run `spacebio-satellite privacy` for the full statement.

## Install

You need Python 3.10 or newer.

```bash
pipx install spacebio-satellite          # recommended: its own isolated environment
# or
pip install spacebio-satellite
```

From a clone of this repository:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Two commands are installed and are identical: `spacebio-satellite` and the short `sbsat`.

## First steps

```bash
spacebio-satellite privacy            # 1. read what leaves your computer
spacebio-satellite login              # 2. connect this computer (you approve in your browser)
spacebio-satellite run --dry-run      # 3. see what one task would send, sending nothing
spacebio-satellite run --for 30m      # 4. work for half an hour
spacebio-satellite status             # your points, limits and outbox
```

**Login** uses the *device flow*: the terminal shows an address and a short code, you open the address in your browser (where you are already signed in to SpaceBio with Google), check the code, read the consent text and approve. The terminal then receives a token. You never type a password in the terminal.

## Commands

| Command | What it does |
|---|---|
| `login [--label NAME] [--no-browser] [--allow-plaintext-token]` | Connect this computer. `--label` names it in your devices list on the website. |
| `logout [--local-only]` | Revoke this device on the server and delete the local token. |
| `status` | Your account state, tier, points today and left, focus, outbox and local totals. |
| `run [options]` | Work. See below. |
| `focus [WORDS...] [--clear]` | Show or set the topics you prefer (kept on your account). |
| `outbox [--flush] [--clear [--yes]]` | List, send or discard batches saved locally and not yet confirmed. |
| `history [-n N]` | What the core decided about your recent batches. |
| `privacy` | What leaves your computer. |
| `doctor` | Check Python, keyring, folders, server, version, clock, login and outbox. |
| `version` | Print the version. |

Options valid for every command, before or after it: `--server URL`, `--json`, `-q/--quiet`, `-v/--verbose`.

### `run`

```bash
spacebio-satellite run --for 1h30m                       # stop starting tasks after 90 minutes
spacebio-satellite run --max-tasks 5                      # five tasks, then stop
spacebio-satellite run --once                             # ask once; stop even if there is nothing to do
spacebio-satellite run --focus radiation,plants --focus-only
spacebio-satellite run --skip-source openalex
spacebio-satellite run --dry-run --show-payload           # fetch one task, print the exact batch, send nothing
```

| Option | Meaning |
|---|---|
| `--for TIME` | Work this long (`30m`, `2h`, `1h30m`, `90s`; a unit is always required). No new task starts after it; the one in hand finishes. |
| `--max-tasks N` | Stop after N tasks. |
| `--once` | One claim, then stop. |
| `--dry-run` | Fetch and clean one task but **send nothing and lease nothing**. Pending batches of earlier runs are left alone. |
| `--show-payload` | With `--dry-run`, print the batch as it would be sent. |
| `--focus WORDS` | Prefer tasks about these keywords (comma separated, repeatable). `--focus-only` accepts nothing else. |
| `--skip-source NAME` | Never take tasks for this database (`europepmc`, `pubmed`, `crossref`, `openalex`). |
| `--no-wait` | Do not wait for the verdict after sending; it is collected on the next run. |
| `--max-batch-records N` | Send at most N records per batch (the server's limit still applies). |

With no limit given, `run` works until you press **Ctrl-C**. The first Ctrl-C finishes cleanly (the task in hand is given back and nothing is lost); a second one stops at once.

### Scripts

`--json` prints one JSON object per line on stdout: events (`login_code`, `batch`, `dry_run`) and a final result. Messages for humans go to stderr as usual. Combine with the exit codes below.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Other failure (also: `doctor` found a failed check) |
| 2 | Wrong usage (bad option, bad duration) |
| 3 | You need to log in (no token, or it expired or was revoked) |
| 4 | Not allowed: account suspended, consent missing or outdated, login refused |
| 5 | This program is too old for the server: update it |
| 6 | Network problem or the server is unavailable |
| 130 | You stopped it with Ctrl-C |

## Where things are stored

| What | Where |
|---|---|
| Access token | Your OS keyring (macOS Keychain, Windows Credential Locker, Secret Service/KWallet on Linux). Never in the settings file. |
| Settings (server, device label, token expiry; **no secrets**) | `config.json` in the platform config folder (`~/Library/Application Support/spacebio-satellite`, `~/.config/spacebio-satellite`, `%APPDATA%\spacebio-satellite`...) |
| Outbox and history | `outbox.sqlite3` in the platform data folder |

If your machine has no usable keyring (a headless server, a container) `login` refuses to store the token unless you choose: set the `SPACEBIO_SATELLITE_TOKEN` environment variable, or pass `--allow-plaintext-token` to keep it in a file only you can read (mode 0600). `doctor` tells you which situation you are in.

### Environment variables

| Variable | Use |
|---|---|
| `SPACEBIO_SATELLITE_URL` | The server (default `https://spacebio.space`). Plain `http://` is accepted only for `localhost`. |
| `SPACEBIO_SATELLITE_TOKEN` | A token to use instead of the keyring (headless machines). |
| `SPACEBIO_SATELLITE_HOME` | Keep config and data in one folder (portable use, tests, demos). |
| `OPENALEX_API_KEY` | Your own free OpenAlex key. **OpenAlex now needs one to read more than the first page of a search.** Without it the program still contributes the first page and tells you so. Get one at <https://openalex.org/settings/api>. |
| `NCBI_API_KEY` | Your own NCBI key (optional; raises PubMed's rate limit). |

Your API keys are added **only** to requests that go straight to their own database. They are never sent to SpaceBio and never appear in any output.

Behind a proxy? The standard `HTTPS_PROXY` / `HTTP_PROXY` variables are honoured.

## Troubleshooting

Start with `spacebio-satellite doctor`.

* **"No secure credential manager is available"**: see *Where things are stored* above.
* **"clock … apart from the server"**: leases depend on time; fix your system clock.
* **Exit 5 / "update"**: `pipx upgrade spacebio-satellite`.
* **OpenAlex warnings about a key**: set `OPENALEX_API_KEY`, or ignore them (the first page is still contributed) or add `--skip-source openalex`.
* **A batch stays in the outbox**: the network or server was unavailable. `spacebio-satellite outbox --flush` sends it; resending is always safe.
* **"rejected … verification_failed"**: a record did not match its source. This counts as a strike (three suspend the account). Please open an issue if you did not modify the program.

## Try it without the real server

A stand-in core and stand-in databases (serving real recorded answers) ship with the repository:

```bash
python devtools/mock_core.py --port 8765 --tasks 6        # terminal 1

export SPACEBIO_SATELLITE_URL=http://127.0.0.1:8765         # terminal 2
export SPACEBIO_SATELLITE_HOME=/tmp/satellite-demo          # keeps the demo apart from your real settings
spacebio-satellite login --no-browser --allow-plaintext-token
spacebio-satellite run --max-tasks 4
spacebio-satellite history
```

## Development

```bash
pip install -e ".[dev]"
pytest                       # ~230 tests, no network, never touches your real keyring
```

* `src/spacebio_satellite/`: `cli` (commands), `api` (the contract's endpoints), `auth` (device flow), `runner` (the work loop), `sources/` (one reader per database), `fetcher` (the only code that talks to the databases), `cleaning` (`paper/1` rules), `outbox` (SQLite), `credentials`, `config`, `output`, `privacy`, `errors`.
* [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md): how the pieces fit, and how to add a database or a task kind.
* [`docs/CONTRACT.md`](docs/CONTRACT.md): the API contract with the SpaceBio core (a mirror; the core owns it).
* `tests/fixtures/`: real answers recorded from the four databases. The readers are tested against them, not against invented data.

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

## Roadmap (not implemented)

The contract reserves `complete/1` (fill in missing abstracts or identifiers), `embed/1` (compute meaning vectors on your machine, for trusted contributors only, with a sample re-checked by the core) and `mirror/1` (shards of the research-data mirrors). This version runs `harvest/1` only and gives back any other kind it is handed.

## License

MIT (see [LICENSE](LICENSE)).
