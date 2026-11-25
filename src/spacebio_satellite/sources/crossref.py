"""Crossref (https://api.crossref.org): JSON, cursor pagination."""
from __future__ import annotations

from ..util import has_phrase, text_of
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


GIVE_UP_AFTER = 3                      # pages in a row without a single paper that has the phrase: Crossref ranks by loose relevance, so more pages will not change that


class Crossref(Source):
    """Crossref has NO phrase search: its API answers the same 100,000+ papers with and without quotes. So the keyword is checked here, against the title and the
    abstract: a paper that does not contain the phrase is left out instead of being sent for the server to throw away (``left_out`` counts them)."""

    name = "crossref"
    hosts = ("api.crossref.org",)

    def __init__(self):
        self.left_out = 0

    def collect(self, task, http, *, max_records, tick=None, notes=None):
        url, pages, _size = self.request_of(task)
        phrase = str(task.get("keyword") or "")
        out, self.left_out, barren = [], 0, 0
        for page in range(pages):
            if tick:
                tick(page)
            records, cursor = parse_page(http.get_json(url))
            kept = [r for r in records if not phrase or has_phrase(phrase, r["title"], r["abstract"])]
            self.left_out += len(records) - len(kept)
            out += kept
            barren = 0 if kept else barren + 1
            if not records or not cursor or len(out) >= max_records or (phrase and barren >= GIVE_UP_AFTER):
                break
            url = set_param(url, "cursor", cursor)
        return out
