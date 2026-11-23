"""What every source has in common: read the answer of a database page by page, turn each paper into the raw ``paper/1`` shape."""
from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


def get_param(url: str, key: str, default: str = "") -> str:
    return dict(parse_qsl(urlparse(url).query, keep_blank_values=True)).get(key, default)


def set_param(url: str, key: str, value) -> str:
    parsed = urlparse(url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query = [(k, v) for k, v in query if k != key] + [(key, str(value))]
    return urlunparse(parsed._replace(query=urlencode(query)))


def rebuild_abstract(index) -> str:
    """OpenAlex stores abstracts as {word: [positions]}."""
    words = {}
    for word, positions in (index or {}).items():
        for pos in positions or []:
            words[pos] = word
    return " ".join(words[p] for p in sorted(words))


class Source:
    """Subclasses set ``name`` and ``hosts`` and implement ``collect``."""

    name = ""
    hosts: tuple = ()

    def collect(self, task: dict, http, *, max_records: int, tick=None, notes=None) -> list[dict]:
        """Run the task: follow the pages the database offers, at most ``task['request']['pages']``. Returns raw records.
        ``notes`` (a list) receives messages meant for the person, for example that only the first page could be read."""
        raise NotImplementedError

    # ---------------------------------------------------------------------------------------------- shared pieces
    @staticmethod
    def request_of(task: dict):
        req = task.get("request") or {}
        return str(req.get("url") or ""), max(1, int(req.get("pages") or 1)), max(1, int(req.get("page_size") or 100))
