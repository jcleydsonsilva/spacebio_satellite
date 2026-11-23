"""The readers of the four databases, checked against REAL answers recorded in tests/fixtures, and the page-following logic against the mock core."""
import json
from pathlib import Path

import pytest

from spacebio_satellite import cleaning
from spacebio_satellite.errors import SourceError
from spacebio_satellite.fetcher import SourceHttp
from spacebio_satellite.sources import SOURCES, crossref, europepmc, get_source, openalex, pubmed
from spacebio_satellite.sources.base import get_param, rebuild_abstract, set_param

FIX = Path(__file__).parent / "fixtures"


def load(name):
    text = (FIX / name).read_text(encoding="utf-8")
    return json.loads(text) if name.endswith(".json") else text


# ---------------------------------------------------------------------------------------------------- the real answers
def test_europepmc_real_answer():
    records, cursor = europepmc.parse_page(load("europepmc.json"))
    assert len(records) == 3 and cursor
    first = records[0]
    assert first["doi"] == "10.1038/s41526-023-00336-6" and first["journal"] == "NPJ microgravity" and first["year"] == "2023"
    assert first["title"] and "<" not in first["title"] and first["authors"] and first["abstract"]
    assert all(cleaning.clean_record(r) for r in records), "every real record must survive cleaning"


def test_europepmc_only_open_or_free_links_are_kept():
    data = {"resultList": {"result": [{"doi": "10.1234/x.1", "title": "A title long enough to pass", "fullTextUrlList": {"fullTextUrl": [
        {"availabilityCode": "S", "url": "https://paywalled.example/1"}, {"availabilityCode": "OA", "url": "https://open.example/2"}, {"availabilityCode": "F", "url": "https://free.example/3"}]}}]}}
    records, _ = europepmc.parse_page(data)
    assert records[0]["urls"] == ["https://open.example/2", "https://free.example/3"]


def test_crossref_real_answer():
    records, cursor = crossref.parse_page(load("crossref.json"))
    assert len(records) == 3 and cursor
    assert records[0]["doi"] == "10.32920/ryerson.14637570" and records[0]["year"] == "2023"
    assert "jats" not in records[0]["abstract"] and "<" not in records[0]["abstract"] and records[0]["abstract"].startswith("A numerical study")
    assert records[0]["authors"] and "," in records[0]["authors"] or " " in records[0]["authors"]
    assert all(cleaning.clean_record(r) for r in records)


def test_openalex_real_answer():
    records, cursor = openalex.parse_page(load("openalex.json"))
    assert len(records) == 3 and cursor
    first = records[0]
    assert first["doi"] == "https://doi.org/10.1098/rsif.2022.0869" and first["pmid"] == "36789512" and first["year"] == "2023"
    assert first["abstract"] and first["urls"] == ["https://doi.org/10.1098/rsif.2022.0869"]
    cleaned = cleaning.clean_record(first)
    assert cleaned["doi"] == "10.1098/rsif.2022.0869"                 # the URL form is normalised by the cleaning step
    assert all(cleaning.clean_record(r) for r in records)


def test_pubmed_real_xml():
    records = pubmed.parse_xml(load("pubmed_fetch.xml"))
    assert [r["pmid"] for r in records] == ["39355042", "38288353", "38274779"]
    first = records[0]
    assert first["doi"] == "10.3389/fnume.2023.1225034" and first["year"] == "2023" and first["journal"] == "Frontiers in nuclear medicine"
    assert first["abstract"].startswith("Ionizing radiation") and first["title"].startswith("Evaluation of deep space exploration")
    assert all(cleaning.clean_record(r) for r in records)


def test_pubmed_xml_with_entity_bomb_or_garbage_is_refused():
    bomb = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]><PubmedArticleSet>&b;</PubmedArticleSet>'
    with pytest.raises(SourceError):
        pubmed.parse_xml(bomb)
    with pytest.raises(SourceError):
        pubmed.parse_xml("this is not xml")


def test_pubmed_labelled_abstract_and_medline_date():
    xml = """<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>1</PMID><Article><Journal><JournalIssue><PubDate><MedlineDate>2019 Jan-Feb</MedlineDate></PubDate></JournalIssue>
    <Title>J</Title></Journal><ArticleTitle>A title with <i>italic</i> part</ArticleTitle><Abstract><AbstractText Label="BACKGROUND">First.</AbstractText>
    <AbstractText Label="RESULTS">Second.</AbstractText></Abstract><Language>eng</Language></Article></MedlineCitation></PubmedArticle></PubmedArticleSet>"""
    rec = pubmed.parse_xml(xml)[0]
    assert rec["year"] == "2019" and rec["abstract"] == "BACKGROUND: First. RESULTS: Second." and rec["title"] == "A title with italic part"


def test_rebuild_abstract_orders_words_by_position():
    assert rebuild_abstract({"world": [1], "Hello": [0], "again": [2, 3]}) == "Hello world again again"
    assert rebuild_abstract(None) == ""


def test_parsers_survive_garbage():
    for parse in (europepmc.parse_page, crossref.parse_page, openalex.parse_page):
        assert parse(None)[0] == [] and parse([])[0] == [] and parse({"unexpected": 1})[0] == [] and parse("text")[0] == []


def test_url_param_helpers_replace_and_keep_the_rest():
    url = "https://x.org/a?query=a+b&cursorMark=*&pageSize=2"
    assert get_param(url, "cursorMark") == "*" and get_param(url, "nope", "d") == "d"
    moved = set_param(url, "cursorMark", "AoI=")
    assert get_param(moved, "cursorMark") == "AoI=" and get_param(moved, "pageSize") == "2" and moved.count("cursorMark") == 1


def test_registry():
    assert set(SOURCES) == {"europepmc", "pubmed", "crossref", "openalex"} and get_source("EuropePMC").name == "europepmc" and get_source("scopus") is None


# ---------------------------------------------------------------------------------------------------- following the pages (mock core)
def collect(core, source, **kw):
    task = core.make_task(source, **kw)
    http = SourceHttp(["127.0.0.1"], min_interval_ms=0)
    return get_source(source).collect(task, http, max_records=kw.pop("cap", 1000)), http


def test_europepmc_follows_the_cursor_and_stops_when_it_stops_moving(core):
    records, http = collect(core, "europepmc")
    assert len(records) == 3 and http.requests_made == 2
    assert [get_param(r["path"] + "?" + "&".join(f"{k}={v}" for k, v in r["query"].items()), "cursorMark") for r in core.source_log] == ["*", "c2"]


def test_crossref_stops_on_an_empty_page(core):
    records, http = collect(core, "crossref", pages=5)
    assert len(records) == 3 and http.requests_made == 3


def test_openalex_stops_without_a_next_cursor(core):
    records, http = collect(core, "openalex", pages=5)
    assert len(records) == 3 and http.requests_made == 2


def test_pubmed_pages_through_esearch_and_fetches_each_page(core):
    records, http = collect(core, "pubmed", pages=5, page_size=2)
    assert [r["pmid"] for r in records] == ["39355042", "38288353", "38274779"]
    paths = [r["path"].rsplit("/", 1)[1] for r in core.source_log]
    assert paths == ["esearch.fcgi", "efetch.fcgi", "esearch.fcgi", "efetch.fcgi"]
    fetch = [r for r in core.source_log if r["path"].endswith("efetch.fcgi")][0]
    assert fetch["query"]["tool"] == "SpaceBio" and fetch["query"]["email"] == "project@example.org" and fetch["query"]["db"] == "pubmed"


def test_the_page_limit_of_the_task_is_respected(core):
    records, http = collect(core, "europepmc", pages=1)
    assert http.requests_made == 1 and len(records) == 2


def test_the_record_cap_stops_early(core):
    task = core.make_task("crossref", pages=5)
    http = SourceHttp(["127.0.0.1"], min_interval_ms=0)
    records = get_source("crossref").collect(task, http, max_records=2)
    assert len(records) == 2 and http.requests_made == 1


def test_tick_is_called_before_every_page(core):
    seen = []
    task = core.make_task("europepmc", pages=3)
    get_source("europepmc").collect(task, SourceHttp(["127.0.0.1"], min_interval_ms=0), max_records=100, tick=seen.append)
    assert seen == [0, 1]


def test_openalex_without_a_key_delivers_the_first_page_and_says_why_the_rest_is_missing(core):
    core.paging_needs_key.add("openalex")
    task = core.make_task("openalex", pages=5)
    notes = []
    records = get_source("openalex").collect(task, SourceHttp(["127.0.0.1"], min_interval_ms=0), max_records=100, notes=notes)
    assert len(records) == 2 and len(notes) == 1 and "OPENALEX_API_KEY" in notes[0] and "first page" in notes[0]


def test_openalex_refusing_the_very_first_page_is_an_error_not_a_silent_empty_result(core):
    core.source_status["openalex"] = 401
    task = core.make_task("openalex", pages=2)
    with pytest.raises(SourceError) as caught:
        get_source("openalex").collect(task, SourceHttp(["127.0.0.1"], min_interval_ms=0), max_records=100, notes=[])
    assert caught.value.code == "source_needs_key"
