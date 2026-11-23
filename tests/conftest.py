"""Shared test setup.

Every test runs with: its own SPACEBIO_SATELLITE_HOME, an in-memory keyring (the real one is NEVER touched), no API keys from your
environment, and a fake clock so nothing really sleeps.
"""
import sys
from pathlib import Path

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))                       # so ``devtools.mock_core`` can be imported

from devtools.mock_core import MockCore  # noqa: E402
from spacebio_satellite import cli  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
ENV_TO_CLEAR = ("SPACEBIO_SATELLITE_URL", "SPACEBIO_SATELLITE_TOKEN", "SPACEBIO_SATELLITE_ALLOW_HTTP", "SPACEBIO_SATELLITE_HOME", "NCBI_API_KEY", "OPENALEX_API_KEY")


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.items = {}

    def get_password(self, service, username):
        return self.items.get((service, username))

    def set_password(self, service, username, password):
        self.items[(service, username)] = password

    def delete_password(self, service, username):
        if (service, username) not in self.items:
            raise PasswordDeleteError("not found")
        del self.items[(service, username)]


class FakeTime:
    """A clock that only moves when somebody sleeps: loops that wait finish at once."""

    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds

    def clock(self):
        return self.t


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in ENV_TO_CLEAR:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SPACEBIO_SATELLITE_HOME", str(tmp_path / "home"))
    previous = keyring.get_keyring()
    memory = MemoryKeyring()
    keyring.set_keyring(memory)
    monkeypatch.setattr(cli, "DEPS", cli.Deps(sleep=lambda s: None, clock=FakeTime().clock, now=cli.utcnow, opener=lambda url: True))
    yield memory
    keyring.set_keyring(previous)


@pytest.fixture
def memory_keyring(isolated):
    return isolated


@pytest.fixture
def fake_time(monkeypatch):
    ft = FakeTime()
    monkeypatch.setattr(cli, "DEPS", cli.Deps(sleep=ft.sleep, clock=ft.clock, now=cli.utcnow, opener=lambda url: True))
    return ft


@pytest.fixture
def core():
    mock = MockCore(FIXTURES).start()
    yield mock
    mock.stop()


@pytest.fixture
def served(core, monkeypatch):
    """A running mock core that the CLI is pointed at."""
    monkeypatch.setenv("SPACEBIO_SATELLITE_URL", core.url)
    return core


@pytest.fixture
def token(core):
    """A token the mock core already knows (for tests that call the API or the runner directly)."""
    value = "sbs_test_token_0123456789"
    core.tokens[value] = "device-1"
    return value


@pytest.fixture
def logged_in(served, fake_time):
    """Log in through the real CLI once, so the keyring and settings hold a valid token."""
    assert cli.main(["login", "--no-browser", "--label", "Test laptop"]) == 0
    return served
