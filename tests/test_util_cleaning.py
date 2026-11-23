import pytest

from spacebio_satellite import cleaning
from spacebio_satellite.util import human_duration, iso, parse_duration, parse_iso, parse_version, text_of


# ---------------------------------------------------------------------------------------------------- util
@pytest.mark.parametrize("text,seconds", [("30m", 1800), ("2h", 7200), ("1h30m", 5400), ("90s", 90), ("1h2m3s", 3723), (" 15M ", 900)])
def test_parse_duration(text, seconds):
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "30", "abc", "0m", "-5m", "1d", "m30", None])
def test_parse_duration_refuses_ambiguous_or_invalid(text):
    with pytest.raises(ValueError):
        parse_duration(text)


def test_parse_version_and_order():
    assert parse_version("0.10.2") == (0, 10, 2)
    assert parse_version("1.2") == (1, 2, 0)
    assert parse_version("") == (0, 0, 0)
    assert parse_version("0.10.0") > parse_version("0.9.9")        # numbers, not text: 10 > 9


def test_human_duration():
    assert human_duration(5) == "5s" and human_duration(125) == "2m05s" and human_duration(3725) == "1h02m"


def test_iso_roundtrip_and_parse_iso_edges():
    moment = parse_iso("2026-11-23T01:15:00Z")
    assert moment.tzinfo is not None and iso(moment) == "2026-11-23T01:15:00Z"
    assert parse_iso("2026-11-23T01:15:00") is not None            # no zone: treated as UTC
    assert parse_iso("") is None and parse_iso("not a date") is None and parse_iso(None) is None


def test_text_of_strips_markup_and_control_characters():
    assert text_of("<jats:p>Hello\x00 <b>world</b>\n\n  again</jats:p>") == "Hello world again"
    assert text_of(None) == ""


# ---------------------------------------------------------------------------------------------------- cleaning
def raw(**kw):
    base = {"title": "Microgravity changes gene expression in human cells", "doi": "10.1234/abc.1", "pmid": "", "abstract": "An abstract.", "authors": "A. One, B. Two",
            "journal": "Journal of Tests", "year": "2023", "language": "eng", "urls": ["https://example.org/x"]}
    base.update(kw)
    return base


def test_a_good_record_comes_out_in_paper_1_shape():
    rec = cleaning.clean_record(raw())
    assert set(rec) == {"title", "doi", "pmid", "abstract", "authors", "journal", "year", "language", "urls"}
    assert rec["doi"] == "10.1234/abc.1" and rec["year"] == "2023" and rec["urls"] == ["https://example.org/x"]


@pytest.mark.parametrize("given,expected", [("https://doi.org/10.1000/ABC.", "10.1000/abc"), ("doi:10.1000/x", "10.1000/x"), ("https://openalex.org/W1", ""), ("", ""), ("10.1/x", "10.1/x")])
def test_normalise_doi(given, expected):
    assert cleaning.normalise_doi(given) == expected


def test_doi_must_look_like_a_doi_else_it_is_dropped_and_the_pmid_can_save_the_record():
    assert cleaning.clean_record(raw(doi="10.1/x", pmid="")) is None            # 10.1 is not a valid registrant prefix, and no pmid
    saved = cleaning.clean_record(raw(doi="not-a-doi", pmid="PMID: 12345678"))
    assert saved["doi"] == "" and saved["pmid"] == "12345678"


@pytest.mark.parametrize("bad", [None, "text", [], {}, raw(title="short"), raw(title=""), raw(doi="", pmid="")])
def test_unusable_records_are_dropped(bad):
    assert cleaning.clean_record(bad) is None


def test_markup_and_control_characters_are_removed_everywhere():
    rec = cleaning.clean_record(raw(title="<i>Gene</i> expression\x00 in <b>space</b> flight", abstract="<jats:p>Text\x07 here</jats:p>"))
    assert rec["title"] == "Gene expression in space flight" and rec["abstract"] == "Text here"


def test_year_outside_a_sane_range_is_blank():
    assert cleaning.clean_record(raw(year="1650"))["year"] == ""
    assert cleaning.clean_record(raw(year="2999"))["year"] == ""
    assert cleaning.clean_record(raw(year="2023-05-01"))["year"] == "2023"


def test_only_http_urls_count_and_at_most_five():
    urls = ["https://a.org/1", "javascript:alert(1)", "ftp://x/y", "http://b.org/2", "https://c.org/3", "https://d.org/4", "https://e.org/5", "https://f.org/6"]
    out = cleaning.clean_record(raw(urls=urls))["urls"]
    assert "javascript:alert(1)" not in out and "ftp://x/y" not in out and len(out) <= 5


def test_field_limits_match_the_contract():
    rec = cleaning.clean_record(raw(title="T" * 900, abstract="a" * 20000, authors="x" * 5000, journal="j" * 900, language="l" * 40))
    assert len(rec["title"]) == 500 and len(rec["abstract"]) == 15000 and len(rec["authors"]) == 2000 and len(rec["journal"]) == 300 and len(rec["language"]) == 12


def test_clean_records_counts_invalid_duplicates_and_truncation():
    items = [raw(), raw(), raw(doi="10.1234/other.2", title="Another interesting paper about space"), raw(title="x"), raw(doi="10.1234/third.3", title="A third paper on radiation biology")]
    records, stats = cleaning.clean_records(items, max_records=2)
    assert len(records) == 2
    assert stats == {"received": 5, "invalid": 1, "duplicates": 1, "truncated": 1}


def test_duplicates_are_found_by_doi_or_pmid():
    records, stats = cleaning.clean_records([raw(doi="", pmid="111"), raw(doi="", pmid="111", title="A different title but the same PMID number")], max_records=10)
    assert len(records) == 1 and stats["duplicates"] == 1
