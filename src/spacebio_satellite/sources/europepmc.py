"""Europe PMC (https://europepmc.org/RestfulWebService): JSON, cursor pagination."""
from __future__ import annotations

from ..util import text_of
from .base import Source, get_param, set_param


def parse_page(data):
    """One answer -> ``(records, next_cursor)``."""
    data = data if isinstance(data, dict) else {}
    results = (data.get("resultList") or {}).get("result") or []
    records = []
    for r in results:
        if not isinstance(r, dict):
            continue
        info = r.get("journalInfo") or {}
        urls = [u.get("url") for u in (r.get("fullTextUrlList") or {}).get("fullTextUrl") or [] if u.get("availabilityCode") in ("OA", "F") and u.get("url")]
        records.append({
            "doi": r.get("doi") or "", "pmid": r.get("pmid") or "", "title": text_of(r.get("title")), "abstract": text_of(r.get("abstractText")),
            "authors": text_of(r.get("authorString")), "journal": text_of((info.get("journal") or {}).get("title")),
            "year": str(r.get("pubYear") or info.get("yearOfPublication") or ""), "language": r.get("language") or "", "urls": urls[:3],
        })
    return records, data.get("nextCursorMark")


class EuropePMC(Source):
    name = "europepmc"
    hosts = ("www.ebi.ac.uk",)

    def collect(self, task, http, *, max_records, tick=None, notes=None):
        url, pages, _size = self.request_of(task)
        out = []
        for page in range(pages):
            if tick:
                tick(page)
            records, cursor = parse_page(http.get_json(url))
            out += records
            if not records or not cursor or cursor == get_param(url, "cursorMark") or len(out) >= max_records:
                break
            url = set_param(url, "cursorMark", cursor)
        return out
