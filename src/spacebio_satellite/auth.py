"""Login: the device flow. You approve in your browser (where you are already signed in), the terminal receives the token.

There is no password to type here: SpaceBio accounts use Google sign-in, so the terminal never sees your credentials.
"""
from __future__ import annotations

import time
import webbrowser

from . import __version__
from .errors import ApiError, LoginDenied, LoginExpired, UpgradeRequired
from .util import parse_version


def check_client_version(meta: dict) -> None:
    """Refuse early when the server says this program is too old (a clear message beats a later 426)."""
    minimum = meta.get("min_client_version")
    if minimum and parse_version(__version__) < parse_version(minimum):
        raise UpgradeRequired("upgrade_required", f"This program is version {__version__}; the server needs {minimum} or newer.")


def login(api, out, *, label, open_browser=True, sleep=time.sleep, clock=time.monotonic, opener=webbrowser.open):
    """Run the device flow. Returns the dict of ``/device/token`` (access_token, expires_at, device_id, scopes)."""
    check_client_version(api.meta())
    grant = api.device_code(label)
    uri = grant.get("verification_uri_complete") or grant.get("verification_uri") or ""
    out.say("To connect this computer to SpaceBio:")
    out.say(f"  1. Open  {grant.get('verification_uri', uri)}")
    out.say(f"  2. Check that the code is  {grant.get('user_code', '')}")
    out.say("  3. Read what leaves your computer (run: spacebio-satellite privacy), then approve.")
    out.event("login_code", user_code=grant.get("user_code"), verification_uri=grant.get("verification_uri"), verification_uri_complete=uri)
    if open_browser and uri:
        try:
            opener(uri)
        except Exception:                                      # no browser on this machine: the instructions above are enough
            pass
    interval = max(1, int(grant.get("interval") or 5))
    deadline = clock() + int(grant.get("expires_in") or 600)
    out.say("Waiting for your approval...")
    while True:
        if clock() > deadline:
            raise LoginExpired("The code expired before it was approved.")
        sleep(interval)
        try:
            return api.device_token(grant["device_code"])
        except ApiError as exc:
            if exc.code == "authorization_pending":
                continue
            if exc.code == "slow_down":
                interval += 5
                continue
            if exc.code == "access_denied":
                raise LoginDenied("You (or an administrator) refused this device.") from exc
            if exc.code == "expired_token":
                raise LoginExpired("The code expired before it was approved.") from exc
            raise
