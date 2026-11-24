"""Every way the satellite can fail, with the exit code the command line reports for it.

Exit codes:  0 ok · 1 other failure · 2 wrong usage · 3 sign in needed · 4 not allowed (suspended, consent, scope, waiting for approval)
             5 this client is too old · 6 network or server unavailable · 130 stopped by you (Ctrl-C)
"""
from __future__ import annotations


class SatelliteError(Exception):
    exit_code = 1
    hint = ""

    def __init__(self, message="", *, hint=""):
        super().__init__(message)
        if hint:
            self.hint = hint


class ConfigError(SatelliteError):
    exit_code = 1


class UsageError(SatelliteError):
    exit_code = 2


class NotLoggedIn(SatelliteError):
    exit_code = 3
    hint = "Run: spacebio-satellite login"


class LoginDenied(SatelliteError):
    exit_code = 4


class LoginExpired(SatelliteError):
    exit_code = 3
    hint = "The code expired. Run: spacebio-satellite login"


class NetworkError(SatelliteError):
    exit_code = 6


class StopRequested(SatelliteError):
    """The person pressed Ctrl-C (or the process was asked to stop): finish cleanly."""

    exit_code = 130


# ---------------------------------------------------------------------------------------------------- the core's answers
class ApiError(SatelliteError):
    """An error answer of the SpaceBio core: ``{"error": {"code", "message", "retry_after", "details"}}``."""

    def __init__(self, code="error", message="", *, status=0, retry_after=None, details=None, hint=""):
        super().__init__(message or code, hint=hint)
        self.code, self.status, self.retry_after, self.details = code, status, retry_after, details or {}

    def __str__(self):
        return f"{self.args[0]} ({self.code})" if self.args and self.args[0] != self.code else self.code


class InvalidToken(ApiError):
    exit_code = 3
    hint = "The token is missing, expired or revoked. Run: spacebio-satellite login"


class NotConsented(ApiError):
    exit_code = 4
    hint = "Your consent is missing or outdated. Open the Satellite page on the SpaceBio website and accept again."


class Suspended(ApiError):
    exit_code = 4
    hint = "This account is suspended from contributing. Contact the SpaceBio moderators if you think this is a mistake."


class PendingApproval(ApiError):
    """The computer is connected but a SpaceBio administrator has not approved it yet (only when the server requires approval)."""

    exit_code = 4
    hint = "A SpaceBio administrator has to approve this computer. Try again later: `spacebio-satellite status` shows when it is approved."


class InsufficientScope(ApiError):
    exit_code = 4


class TaskExpired(ApiError):
    exit_code = 1


class DuplicateBatch(ApiError):
    exit_code = 1


class TooLarge(ApiError):
    exit_code = 1


class InvalidPayload(ApiError):
    exit_code = 1


class UpgradeRequired(ApiError):
    exit_code = 5
    hint = "Update this program:  pipx upgrade spacebio-satellite   (or: pip install -U spacebio-satellite)"


class RateLimited(ApiError):
    exit_code = 6


class Unavailable(ApiError):
    exit_code = 6


class UnsupportedApi(ApiError):
    exit_code = 5
    hint = "The server speaks a different API version. Update this program."


_BY_CODE = {
    "invalid_token": InvalidToken, "not_consented": NotConsented, "suspended": Suspended, "insufficient_scope": InsufficientScope, "pending_approval": PendingApproval,
    "task_not_yours_or_expired": TaskExpired, "duplicate_batch": DuplicateBatch, "too_large": TooLarge, "invalid_payload": InvalidPayload,
    "upgrade_required": UpgradeRequired, "rate_limited": RateLimited, "unavailable": Unavailable,
}
_BY_STATUS = {401: InvalidToken, 413: TooLarge, 422: InvalidPayload, 426: UpgradeRequired, 429: RateLimited}


def error_from_response(status, body, headers=None):
    """Turn an HTTP error answer of the core into the right exception (also when the body is not the documented JSON)."""
    headers = headers or {}
    err = (body or {}).get("error") if isinstance(body, dict) else None
    if isinstance(err, str):                                  # tolerate the older flat shape {"error": "code"}
        err = {"code": err}
    err = err if isinstance(err, dict) else {}
    code = str(err.get("code") or "")
    message = str(err.get("message") or "")
    retry = err.get("retry_after")
    if retry is None and headers.get("Retry-After", "").isdigit():
        retry = int(headers["Retry-After"])
    cls = _BY_CODE.get(code) or _BY_STATUS.get(status) or (Unavailable if status >= 500 else ApiError)
    if not code:
        code = {401: "invalid_token", 426: "upgrade_required", 429: "rate_limited"}.get(status, "unavailable" if status >= 500 else "http_%s" % status)
    return cls(code, message, status=status, retry_after=retry, details=err.get("details") if isinstance(err.get("details"), dict) else {})


# ---------------------------------------------------------------------------------------------------- public databases
class SourceError(SatelliteError):
    """Something went wrong while asking a public database (not the SpaceBio core)."""

    exit_code = 6

    def __init__(self, message="", *, busy=False, status=0, retry_after=None, code="source_unavailable"):
        super().__init__(message)
        self.busy, self.status, self.retry_after, self.code = busy, status, retry_after, code


class HostNotAllowed(SourceError):
    """The task asked for a host it did not list as allowed: never contact it."""

    def __init__(self, message=""):
        super().__init__(message, code="host_not_allowed")
