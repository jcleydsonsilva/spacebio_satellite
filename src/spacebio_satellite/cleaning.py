"""Clean the records before they leave the computer: the same rules the SpaceBio core applies (``paper/1``).

The core cleans again and never trusts what it receives, so this is a courtesy that keeps batches small and drops records that
would be refused anyway. Nothing here decides relevance, duplicates or points: those are the core's.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from .util import text_of

LIMITS = {"title": 500, "abstract": 15000, "authors": 2000, "journal": 300}
MAX_URLS, MAX_URL_LENGTH = 5, 400
MIN_TITLE = 15
_DOI = re.compile(r"^10\.\d{4,9}/\S+$")
_DOI_PREFIX = re.compile(r"^(https?://)?(dx\.)?doi\.org/|^doi:\s*", re.I)


def normalise_doi(value) -> str:
    """'https://doi.org/10.1000/ABC.' -> '10.1000/abc' ('' when it is not a DOI, e.g. an OpenAlex URL)."""
    text = _DOI_PREFIX.sub("", str(value or "").strip()).strip().rstrip(".,;)").lower()
    return text if text.startswith("10.") and "/" in text else ""


def clean_record(raw, now_year=None):
    """One raw record -> a ``paper/1`` dict, or ``None`` when it is unusable (no title, or neither a DOI nor a PMID)."""
    if not isinstance(raw, dict):
        return None
    now_year = now_year or datetime.now(timezone.utc).year
    title = text_of(raw.get("title"))[: LIMITS["title"]]
    doi = normalise_doi(text_of(raw.get("doi"))[:200])
    doi = doi if _DOI.match(doi) else ""
    pmid = re.sub(r"\D", "", str(raw.get("pmid") or ""))[:9]
    if len(title) < MIN_TITLE or not (doi or pmid):
        return None
    year = str(raw.get("year") or "")[:4]
    year = year if year.isdigit() and 1800 <= int(year) <= now_year + 1 else ""
    urls = []
    for url in (raw.get("urls") or [])[:MAX_URLS]:
        url = str(url or "").strip()[:MAX_URL_LENGTH]
        if url.startswith(("https://", "http://")):
            urls.append(url)
    return {
        "title": title, "doi": doi, "pmid": pmid, "abstract": text_of(raw.get("abstract"))[: LIMITS["abstract"]],
        "authors": text_of(raw.get("authors"))[: LIMITS["authors"]], "journal": text_of(raw.get("journal"))[: LIMITS["journal"]],
        "year": year, "language": text_of(raw.get("language"))[:12], "urls": urls,
    }


def clean_records(raws, *, max_records, now_year=None):
    """Clean, drop duplicates (same DOI or PMID) and cap the count. Returns ``(records, stats)``."""
    out, seen = [], set()
    stats = {"received": 0, "invalid": 0, "duplicates": 0, "truncated": 0}
    for raw in raws:
        stats["received"] += 1
        record = clean_record(raw, now_year)
        if record is None:
            stats["invalid"] += 1
            continue
        key = record["doi"] or f"pmid:{record['pmid']}"
        if key in seen:
            stats["duplicates"] += 1
            continue
        seen.add(key)
        if len(out) >= max_records:
            stats["truncated"] += 1
            continue
        out.append(record)
    return out, stats
