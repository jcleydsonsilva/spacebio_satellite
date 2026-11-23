"""Where the access token is kept: the operating system's credential manager (keyring), never a plain file by default.

Order of lookup: the SPACEBIO_SATELLITE_TOKEN environment variable (headless machines), then the keyring, then (only if the
person explicitly accepted it at login) a private file. A plain file is mode 0600 and the program says so out loud.
"""
from __future__ import annotations

import json
import os

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

from .config import ENV_TOKEN, Paths, write_private
from .errors import ConfigError

SERVICE = "spacebio-satellite"
# Backends that do not protect anything: a keyring that is not there, or one that writes the secret in clear text.
_INSECURE = ("keyring.backends.fail.Keyring", "keyring.backends.null.Keyring", "keyrings.alt.file.PlaintextKeyring")


def _backend_names() -> list[str]:
    """The names of the backends that would really be used (the chainer lists the viable ones)."""
    try:
        backend = keyring.get_keyring()
    except Exception:                                      # pragma: no cover - a broken backend must not crash the CLI
        return ["keyring.backends.fail.Keyring"]
    inner = getattr(backend, "backends", None)
    chosen = [backend] if not inner else list(inner)
    return [f"{type(b).__module__}.{type(b).__name__}" for b in chosen] or ["keyring.backends.fail.Keyring"]


def keyring_is_secure() -> bool:
    """True when the backend that would be used (the first viable one, by priority) really protects the secret."""
    names = _backend_names()
    return bool(names) and names[0] not in _INSECURE


def keyring_description() -> str:
    return ", ".join(_backend_names())


class TokenStore:
    def __init__(self, paths: Paths):
        self.paths = paths

    # ---------------------------------------------------------------------------------------------- read
    def get(self, server: str) -> tuple[str, str]:
        """(token, where) with where in 'env', 'keyring', 'file'; ('', '') when there is none."""
        env = os.environ.get(ENV_TOKEN, "").strip()
        if env:
            return env, "env"
        try:
            value = keyring.get_password(SERVICE, server)
        except KeyringError:
            value = None
        if value:
            return value, "keyring"
        value = self._file_read().get(server, "")
        return (value, "file") if value else ("", "")

    # ---------------------------------------------------------------------------------------------- write
    def set(self, server: str, token: str, *, allow_plaintext: bool = False) -> str:
        """Store the token and return where ('keyring' or 'file'). Refuses an insecure place unless the person allowed it."""
        if keyring_is_secure():
            try:
                keyring.set_password(SERVICE, server, token)
                return "keyring"
            except KeyringError as exc:
                if not allow_plaintext:
                    raise ConfigError(f"The credential manager refused to store the token ({exc}).", hint=self._advice()) from exc
        elif not allow_plaintext:
            raise ConfigError("No secure credential manager is available on this computer, so the token cannot be stored safely.", hint=self._advice())
        data = self._file_read()
        data[server] = token
        write_private(self.paths.token_file, json.dumps(data))
        return "file"

    def delete(self, server: str) -> None:
        try:
            keyring.delete_password(SERVICE, server)
        except (PasswordDeleteError, KeyringError):
            pass
        data = self._file_read()
        if server in data:
            del data[server]
            if data:
                write_private(self.paths.token_file, json.dumps(data))
            else:
                try:
                    self.paths.token_file.unlink()
                except OSError:
                    pass

    # ---------------------------------------------------------------------------------------------- internals
    def _file_read(self) -> dict:
        try:
            raw = json.loads(self.paths.token_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}

    @staticmethod
    def _advice() -> str:
        return ("Options: install/unlock a system keyring; or set the SPACEBIO_SATELLITE_TOKEN environment variable; "
                "or run login with --allow-plaintext-token (the token is then kept in a file only you can read).")
