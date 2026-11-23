import json
import os
import stat

import keyring
import pytest
from keyring.backends import fail

from spacebio_satellite import config, credentials
from spacebio_satellite.config import Device, Settings, get_paths, normalise_server, resolve_server
from spacebio_satellite.credentials import SERVICE, TokenStore
from spacebio_satellite.errors import ConfigError


# ---------------------------------------------------------------------------------------------------- server addresses
@pytest.mark.parametrize("given,expected", [
    ("spacebio.space", "https://spacebio.space"), ("https://SpaceBio.Space/", "https://spacebio.space"), ("http://localhost:8000", "http://localhost:8000"),
    ("http://127.0.0.1:8765/", "http://127.0.0.1:8765"), ("  https://example.org  ", "https://example.org"),
])
def test_normalise_server(given, expected):
    assert normalise_server(given) == expected


@pytest.mark.parametrize("bad", ["", "   ", "ftp://x.org", "https://x.org/some/path", "https://x.org?a=1", "http://spacebio.space"])
def test_bad_server_addresses_are_refused(bad):
    with pytest.raises(ConfigError):
        normalise_server(bad)


def test_plain_http_to_another_host_needs_an_explicit_switch(monkeypatch):
    monkeypatch.setenv("SPACEBIO_SATELLITE_ALLOW_HTTP", "1")
    assert normalise_server("http://staging.example.org") == "http://staging.example.org"


def test_server_precedence_flag_then_env_then_saved_then_default(monkeypatch):
    saved = Settings(default_server="https://saved.example.org")
    assert resolve_server("https://flag.example.org", saved) == "https://flag.example.org"
    monkeypatch.setenv("SPACEBIO_SATELLITE_URL", "https://env.example.org")
    assert resolve_server(None, saved) == "https://env.example.org"
    monkeypatch.delenv("SPACEBIO_SATELLITE_URL")
    assert resolve_server(None, saved) == "https://saved.example.org"
    assert resolve_server(None, Settings()) == config.DEFAULT_SERVER


# ---------------------------------------------------------------------------------------------------- folders and settings
def test_home_variable_moves_everything(tmp_path, monkeypatch):
    monkeypatch.setenv("SPACEBIO_SATELLITE_HOME", str(tmp_path / "h"))
    paths = get_paths()
    assert paths.config_dir == tmp_path / "h" / "config" and paths.outbox_file == tmp_path / "h" / "data" / "outbox.sqlite3"


def test_default_folders_follow_the_platform(monkeypatch):
    monkeypatch.delenv("SPACEBIO_SATELLITE_HOME")
    paths = get_paths()
    assert "spacebio-satellite" in str(paths.config_dir) and "spacebio-satellite" in str(paths.data_dir)


def test_settings_roundtrip_and_private_file():
    paths = get_paths()
    settings = Settings(default_server="https://a.example.org", devices={"https://a.example.org": Device(device_id="d1", label="Home", token_storage="keyring", token_expires_at="2027-01-01T00:00:00Z")})
    settings.save(paths)
    assert stat.S_IMODE(os.stat(paths.settings_file).st_mode) == 0o600
    again = Settings.load(paths)
    assert again.default_server == "https://a.example.org" and again.device("https://a.example.org").label == "Home" and again.device("https://nope").device_id == ""


def test_a_damaged_settings_file_never_blocks_the_program():
    paths = get_paths()
    paths.config_dir.mkdir(parents=True)
    paths.settings_file.write_text("{ this is not json")
    assert Settings.load(paths).devices == {}


def test_no_half_written_files_are_left_behind():
    paths = get_paths()
    Settings().save(paths)
    Settings().save(paths)
    assert sorted(p.name for p in paths.config_dir.iterdir()) == ["config.json"]


# ---------------------------------------------------------------------------------------------------- the token
def test_token_goes_to_the_keyring(memory_keyring):
    store = TokenStore(get_paths())
    assert store.set("https://a.example.org", "sbs_secret") == "keyring"
    assert memory_keyring.items[(SERVICE, "https://a.example.org")] == "sbs_secret"
    assert store.get("https://a.example.org") == ("sbs_secret", "keyring")
    assert not get_paths().token_file.exists()


def test_tokens_are_kept_per_server(memory_keyring):
    store = TokenStore(get_paths())
    store.set("https://a.example.org", "A")
    store.set("https://b.example.org", "B")
    assert store.get("https://a.example.org")[0] == "A" and store.get("https://b.example.org")[0] == "B"
    store.delete("https://a.example.org")
    assert store.get("https://a.example.org") == ("", "") and store.get("https://b.example.org")[0] == "B"


def test_environment_token_wins(memory_keyring, monkeypatch):
    store = TokenStore(get_paths())
    store.set("https://a.example.org", "from-keyring")
    monkeypatch.setenv("SPACEBIO_SATELLITE_TOKEN", "from-env")
    assert store.get("https://a.example.org") == ("from-env", "env")


def test_without_a_secure_keyring_the_token_is_refused_by_default():
    keyring.set_keyring(fail.Keyring())
    assert not credentials.keyring_is_secure()
    with pytest.raises(ConfigError) as caught:
        TokenStore(get_paths()).set("https://a.example.org", "sbs_secret")
    assert "SPACEBIO_SATELLITE_TOKEN" in caught.value.hint and "--allow-plaintext-token" in caught.value.hint
    assert not get_paths().token_file.exists()


def test_plaintext_fallback_is_explicit_private_and_removable():
    keyring.set_keyring(fail.Keyring())
    store = TokenStore(get_paths())
    assert store.set("https://a.example.org", "sbs_secret", allow_plaintext=True) == "file"
    path = get_paths().token_file
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600 and json.loads(path.read_text()) == {"https://a.example.org": "sbs_secret"}
    assert store.get("https://a.example.org") == ("sbs_secret", "file")
    store.delete("https://a.example.org")
    assert not path.exists() and store.get("https://a.example.org") == ("", "")


def test_a_memory_keyring_counts_as_secure_and_the_real_names_are_reported(memory_keyring):
    assert credentials.keyring_is_secure() and "MemoryKeyring" in credentials.keyring_description()


def test_deleting_a_token_that_is_not_there_is_fine(memory_keyring):
    TokenStore(get_paths()).delete("https://never.example.org")
