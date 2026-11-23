"""Crossref (https://api.crossref.org): JSON, cursor pagination."""
from __future__ import annotations

from ..util import text_of
from .base import Source, set_param


def _year(item):
    for key in ("issued", "published-print", "published-online", "published", "created"):
        parts = (item.get(key) or {}).get("date-parts") or [[None]]
        if parts and parts[0] and parts[0][0]:
            return str(parts[0][0])
    return ""


def _author(a):
    name = " ".join(x for x in (a.get("given"), a.get("family")) if x) or a.get("name") or ""
    return text_of(name)


def parse_page(data):
    """One answer -> ``(records, next_cursor)``."""
    message = (data or {}).get("message") if isinstance(data, dict) else None
    message = message if isinstance(message, dict) else {}
    records = []
    for r in message.get("items") or []:
        if not isinstance(r, dict):
            continue
        title = r.get("title")
        title = title[0] if isinstance(title, list) and title else (title if isinstance(title, str) else "")
        container = r.get("container-title")
        container = container[0] if isinstance(container, list) and container else ""
        records.append({
            "doi": r.get("DOI") or "", "pmid": "", "title": text_of(title), "abstract": text_of(r.get("abstract")),
            "authors": ", ".join(x for x in (_author(a) for a in (r.get("author") or [])[:40]) if x), "journal": text_of(container),
            "year": _year(r), "language": r.get("language") or "", "urls": [link.get("URL") for link in (r.get("link") or [])[:2] if link.get("URL")],
        })
    return records, message.get("next-cursor")


class Crossref(Source):
    name = "crossref"
    hosts = ("api.crossref.org",)

    def collect(self, task, http, *, max_records, tick=None, notes=None):
        url, pages, _size = self.request_of(task)
        out = []
        for page in range(pages):
            if tick:
                tick(page)
            records, cursor = parse_page(http.get_json(url))
            out += records
            if not records or not cursor or len(out) >= max_records:
                break
            url = set_param(url, "cursor", cursor)
        return out
