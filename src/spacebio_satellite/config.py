"""Where things live on disk and which server to talk to. Nothing secret is stored here (the token goes to credentials.py)."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import platformdirs

from .errors import ConfigError

APP = "spacebio-satellite"
DEFAULT_SERVER = "https://spacebio.space"
ENV_HOME = "SPACEBIO_SATELLITE_HOME"            # one folder for everything (handy for tests and for portable use)
ENV_URL = "SPACEBIO_SATELLITE_URL"
ENV_TOKEN = "SPACEBIO_SATELLITE_TOKEN"          # a token from the environment wins over the keyring (headless machines)
ENV_ALLOW_HTTP = "SPACEBIO_SATELLITE_ALLOW_HTTP"
LOOPBACK = {"localhost", "127.0.0.1", "::1"}


@dataclass(frozen=True)
class Paths:
    config_dir: Path
    data_dir: Path

    @property
    def settings_file(self) -> Path:
        return self.config_dir / "config.json"

    @property
    def token_file(self) -> Path:
        return self.config_dir / "token.json"

    @property
    def outbox_file(self) -> Path:
        return self.data_dir / "outbox.sqlite3"


def get_paths() -> Paths:
    home = os.environ.get(ENV_HOME)
    if home:
        return Paths(Path(home) / "config", Path(home) / "data")
    return Paths(Path(platformdirs.user_config_dir(APP)), Path(platformdirs.user_data_dir(APP)))


def normalise_server(url: str, *, allow_http: bool | None = None) -> str:
    """'spacebio.space' -> 'https://spacebio.space'. Plain http is refused except for this computer (development)."""
    text = (url or "").strip()
    if not text:
        raise ConfigError("No server address.")
    if "://" not in text:
        text = "https://" + text
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ConfigError(f"Not a valid server address: {url!r}")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ConfigError("Give only the site address (for example https://spacebio.space), without a path.")
    if parsed.scheme == "http":
        if allow_http is None:
            allow_http = os.environ.get(ENV_ALLOW_HTTP) == "1"
        if parsed.hostname not in LOOPBACK and not allow_http:
            raise ConfigError("Refusing a plain http:// server: the token would travel unencrypted. Use https://.",
                              hint="(for a local development server, localhost is allowed)")
    netloc = parsed.netloc.lower()
    return f"{parsed.scheme}://{netloc}"


@dataclass
class Device:
    """What this computer remembers about its registration on one server (never the token itself)."""

    device_id: str = ""
    label: str = ""
    token_storage: str = "keyring"        # "keyring" | "file" | "env"
    token_expires_at: str = ""
    logged_in_at: str = ""


@dataclass
class Settings:
    default_server: str = ""
    devices: dict[str, Device] = field(default_factory=dict)

    @classmethod
    def load(cls, paths: Paths) -> "Settings":
        try:
            raw = json.loads(paths.settings_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        except (OSError, ValueError):
            return cls()                                   # a damaged settings file must never block the program
        devices = {}
        for server, item in (raw.get("devices") or {}).items():
            if isinstance(item, dict):
                devices[server] = Device(**{k: str(item.get(k, "")) for k in Device.__dataclass_fields__})
        return cls(default_server=str(raw.get("default_server") or ""), devices=devices)

    def save(self, paths: Paths) -> None:
        paths.config_dir.mkdir(parents=True, exist_ok=True)
        payload = {"default_server": self.default_server, "devices": {s: vars(d) for s, d in self.devices.items()}}
        write_private(paths.settings_file, json.dumps(payload, indent=2, sort_keys=True))

    def device(self, server: str) -> Device:
        return self.devices.get(server) or Device()


def write_private(path: Path, text: str) -> None:
    """Write a file only the owner can read, replacing it atomically (no half-written file after a crash)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def resolve_server(cli_value: str | None, settings: Settings) -> str:
    """The server to use: --server, else the environment, else the one last logged in to, else the default."""
    chosen = cli_value or os.environ.get(ENV_URL) or settings.default_server or DEFAULT_SERVER
    return normalise_server(chosen)
