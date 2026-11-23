"""OpenAlex (https://docs.openalex.org): JSON, cursor pagination. It asks for a free API key: set OPENALEX_API_KEY in your environment."""
from __future__ import annotations

import re

from ..errors import SourceError
from ..util import text_of
from .base import Source, rebuild_abstract, set_param


def parse_page(data):
    """One answer -> ``(records, next_cursor)``."""
    data = data if isinstance(data, dict) else {}
    records = []
    for r in data.get("results") or []:
        if not isinstance(r, dict):
            continue
        pmid = re.sub(r"\D", "", str((r.get("ids") or {}).get("pmid") or ""))
        source = ((r.get("primary_location") or {}).get("source") or {})
        oa_url = (r.get("open_access") or {}).get("oa_url")
        records.append({
            "doi": r.get("doi") or "", "pmid": pmid, "title": text_of(r.get("title") or r.get("display_name")),
            "abstract": text_of(rebuild_abstract(r.get("abstract_inverted_index"))),
            "authors": ", ".join(x for x in (text_of((a.get("author") or {}).get("display_name")) for a in (r.get("authorships") or [])[:40]) if x),
            "journal": text_of(source.get("display_name")), "year": str(r.get("publication_year") or ""), "language": r.get("language") or "",
            "urls": [oa_url] if oa_url else [],
        })
    return records, (data.get("meta") or {}).get("next_cursor")


class OpenAlex(Source):
    name = "openalex"
    hosts = ("api.openalex.org",)

    def collect(self, task, http, *, max_records, tick=None, notes=None):
        url, pages, _size = self.request_of(task)
        out = []
        for page in range(pages):
            if tick:
                tick(page)
            try:
                records, cursor = parse_page(http.get_json(url))
            except SourceError as exc:
                if exc.code == "source_needs_key" and page > 0 and out:      # OpenAlex lets anybody read page 1, but paging needs a free key
                    if notes is not None:
                        notes.append("OpenAlex needs a free API key to read beyond the first page, so only the first page was read. "
                                     "Get a key at https://openalex.org/settings/api and set OPENALEX_API_KEY to read more.")
                    break
                raise
            out += records
            if not records or not cursor or len(out) >= max_records:
                break
            url = set_param(url, "cursor", cursor)
        return out
