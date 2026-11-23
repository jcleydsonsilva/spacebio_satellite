"""PubMed through NCBI E-utilities: ``esearch`` (JSON) gives the identifiers, ``efetch`` (XML) gives the records."""
from __future__ import annotations

import re
from urllib.parse import urlencode, urljoin

from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException

from ..errors import SourceError
from ..util import text_of
from .base import Source, get_param, set_param


def _text(node) -> str:
    return text_of("".join(node.itertext())) if node is not None else ""


def parse_xml(xml_text: str) -> list[dict]:
    """The XML of ``efetch`` -> raw records. defusedxml refuses entity tricks and never fetches external files."""
    try:
        root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    except (ET.ParseError, DefusedXmlException) as exc:                       # garbage, or an entity/DTD trick: refuse, never expand it
        raise SourceError("PubMed answered something that is not valid (or not safe) XML", code="source_bad_answer") from exc
    records = []
    for art in root.iter("PubmedArticle"):
        article = art.find("MedlineCitation/Article")
        if article is None:
            continue
        parts = []
        for node in article.findall("Abstract/AbstractText"):
            label = node.get("Label")
            parts.append((f"{label}: " if label else "") + _text(node))
        authors = []
        for author in article.findall("AuthorList/Author")[:40]:
            collective = _text(author.find("CollectiveName"))
            name = collective or " ".join(x for x in (_text(author.find("LastName")), _text(author.find("ForeName")) or _text(author.find("Initials"))) if x)
            if name:
                authors.append(name)
        doi = next((_text(n) for n in art.findall("PubmedData/ArticleIdList/ArticleId") if n.get("IdType") == "doi"), "")
        doi = doi or next((_text(n) for n in article.findall("ELocationID") if n.get("EIdType") == "doi"), "")
        pub_date = article.find("Journal/JournalIssue/PubDate")
        year = _text(pub_date.find("Year")) if pub_date is not None else ""
        if not year and pub_date is not None:
            year = (re.search(r"\d{4}", _text(pub_date.find("MedlineDate"))) or [""])[0]
        year = year or _text(article.find("ArticleDate/Year"))
        records.append({
            "doi": doi, "pmid": _text(art.find("MedlineCitation/PMID")), "title": _text(article.find("ArticleTitle")), "abstract": " ".join(parts),
            "authors": ", ".join(authors), "journal": _text(article.find("Journal/Title")), "year": year, "language": _text(article.find("Language")), "urls": [],
        })
    return records


class PubMed(Source):
    name = "pubmed"
    hosts = ("eutils.ncbi.nlm.nih.gov",)

    @staticmethod
    def fetch_url(search_url: str, ids, email: str = "") -> str:
        params = {"db": "pubmed", "retmode": "xml", "tool": "SpaceBio", "id": ",".join(ids)}
        if email:
            params["email"] = email
        return urljoin(search_url, "efetch.fcgi") + "?" + urlencode(params, safe=",")

    def collect(self, task, http, *, max_records, tick=None, notes=None):
        url, pages, size = self.request_of(task)
        email = str(task.get("contact_email") or "")
        out = []
        for page in range(pages):
            if tick:
                tick(page)
            search = (http.get_json(url) or {}).get("esearchresult") or {}
            ids = [str(i) for i in search.get("idlist") or []]
            if ids:
                out += parse_xml(http.get_text(self.fetch_url(url, ids, email)))
            start = int(get_param(url, "retstart", "0") or 0) + size
            count = int(str(search.get("count") or "0") or 0)
            if len(ids) < size or start >= count or len(out) >= max_records:
                break
            url = set_param(url, "retstart", start)
        return out
