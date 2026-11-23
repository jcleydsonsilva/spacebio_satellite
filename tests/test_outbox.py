from spacebio_satellite import outbox as outbox_module
from spacebio_satellite.outbox import Outbox

SERVER = "https://a.example.org"


def batch(cid="c1", n=2):
    return {"client_batch_id": cid, "started_at": "2026-11-23T01:00:00Z", "records": [{"title": f"Título com acentuação {i} — ünïcode", "doi": f"10.1234/{i}"} for i in range(n)]}


def test_a_batch_survives_a_restart_and_keeps_its_unicode(tmp_path):
    path = tmp_path / "o.sqlite3"
    Outbox(path).add(SERVER, 7, batch(), source="europepmc", keyword="microgravity")
    rows = Outbox(path).pending(SERVER)                       # a new object = a new run of the program
    assert len(rows) == 1 and rows[0]["task_id"] == 7 and rows[0]["records"] == 2 and rows[0]["source"] == "europepmc"
    assert rows[0]["batch"]["records"][0]["title"].startswith("Título com acentuação 0") and rows[0]["batch_id"] == ""


def test_sent_failed_and_removed(tmp_path):
    box = Outbox(tmp_path / "o.sqlite3")
    box.add(SERVER, 1, batch())
    box.mark_failed("c1", "network down")
    row = box.pending(SERVER)[0]
    assert row["last_error"] == "network down" and row["attempts"] == 1
    box.mark_sent("c1", "batch-uuid")
    row = box.pending(SERVER)[0]
    assert row["batch_id"] == "batch-uuid" and row["last_error"] == ""
    box.remove("c1")
    assert box.pending(SERVER) == []


def test_servers_do_not_mix(tmp_path):
    box = Outbox(tmp_path / "o.sqlite3")
    box.add(SERVER, 1, batch("a"))
    box.add("https://b.example.org", 2, batch("b"))
    assert [r["client_batch_id"] for r in box.pending(SERVER)] == ["a"]
    assert box.clear(SERVER) == 1 and len(box.pending("https://b.example.org")) == 1


def test_adding_the_same_batch_twice_keeps_one_copy(tmp_path):
    box = Outbox(tmp_path / "o.sqlite3")
    box.add(SERVER, 1, batch())
    box.add(SERVER, 1, batch())
    assert len(box.pending(SERVER)) == 1


def test_pending_comes_oldest_first(tmp_path):
    box = Outbox(tmp_path / "o.sqlite3")
    for cid in ("first", "second", "third"):
        box.add(SERVER, 1, batch(cid))
    assert [r["client_batch_id"] for r in box.pending(SERVER)] == ["first", "second", "third"]


def test_history_totals_and_trimming(tmp_path, monkeypatch):
    monkeypatch.setattr(outbox_module, "KEEP_HISTORY", 3)
    box = Outbox(tmp_path / "o.sqlite3")
    for i in range(5):
        box.record(SERVER, task_id=i, source="crossref", keyword="k", status="accepted" if i % 2 == 0 else "rejected", new=i, completed=1, points=10)
    rows = box.history(SERVER, 10)
    assert [r["task_id"] for r in rows] == [4, 3, 2]                      # newest first, only the last three kept
    totals = box.totals(SERVER)
    assert totals == {"batches": 3, "new": 9, "completed": 3, "points": 30, "rejected": 1}
    assert box.history("https://other.example.org") == []


def test_totals_of_an_empty_history(tmp_path):
    assert Outbox(tmp_path / "o.sqlite3").totals(SERVER) == {"batches": 0, "new": 0, "completed": 0, "points": 0, "rejected": 0}
