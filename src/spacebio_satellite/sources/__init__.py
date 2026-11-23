"""The databases this program can read. The core names the source in each task; an unknown name is given back untouched."""
from __future__ import annotations

from .base import Source
from .crossref import Crossref
from .europepmc import EuropePMC
from .openalex import OpenAlex
from .pubmed import PubMed

SOURCES: dict[str, Source] = {s.name: s for s in (EuropePMC(), PubMed(), Crossref(), OpenAlex())}


def get_source(name: str) -> Source | None:
    return SOURCES.get(str(name or "").lower())


__all__ = ["SOURCES", "Source", "get_source"]
