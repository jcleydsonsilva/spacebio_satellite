"""Small pure helpers: versions, durations, times, text."""
from __future__ import annotations

import re
from datetime import datetime, timezone

_DURATION = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")


def parse_version(text) -> tuple:
    """'0.10.2' -> (0, 10, 2). Missing parts count as 0; anything that is not a number is ignored."""
    nums = [int(n) for n in re.findall(r"\d+", str(text or ""))[:3]]
    return tuple(nums + [0] * (3 - len(nums)))


def parse_duration(text) -> int:
    """'30m' -> 1800, '1h30m' -> 5400, '90s' -> 90. A unit is required so '30' is never ambiguous."""
    value = str(text or "").strip().lower().replace(" ", "")
    match = _DURATION.match(value)
    if not value or not match or not any(match.groups()):
        raise ValueError(f"not a duration: {text!r} (use for example 30m, 2h, 1h30m or 90s)")
    h, m, s = (int(g or 0) for g in match.groups())
    seconds = h * 3600 + m * 60 + s
    if seconds <= 0:
        raise ValueError("the duration must be longer than zero")
    return seconds


def human_duration(seconds) -> str:
    seconds = int(max(0, seconds))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(text) -> datetime | None:
    """Parse the server's ISO 8601 times ('...Z' or with an offset); None when empty or malformed."""
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


_TAGS = re.compile(r"<[^>]*>")
_SPACES = re.compile(r"\s+")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def text_of(value) -> str:
    """Plain text: markup removed, control characters dropped, one space between words."""
    return _SPACES.sub(" ", _TAGS.sub(" ", _CONTROL.sub("", "" if value is None else str(value)))).strip()
