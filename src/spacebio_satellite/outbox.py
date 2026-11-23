"""The local outbox: a small SQLite file (Python's own sqlite3, no server, no Django) that makes a crash harmless.

A batch is written here BEFORE it is sent and removed only after the core's final verdict. If the program stops in between,
the next ``run`` sends it again with the same ``client_batch_id`` (the core never stores it twice) or just asks for its verdict.
It also keeps a short history of what the core decided, shown by ``spacebio-satellite history``.
"""
from __future__ import annotations

import gzip
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from .util import iso, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    client_batch_id TEXT PRIMARY KEY, server TEXT NOT NULL, task_id INTEGER NOT NULL, payload BLOB NOT NULL, records INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, batch_id TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, server TEXT NOT NULL, task_id INTEGER, source TEXT NOT NULL DEFAULT '', keyword TEXT NOT NULL DEFAULT '',
    batch_id TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', received INTEGER NOT NULL DEFAULT 0,
    new INTEGER NOT NULL DEFAULT 0, completed INTEGER NOT NULL DEFAULT 0, points INTEGER NOT NULL DEFAULT 0);
"""
KEEP_HISTORY = 500


class Outbox:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._db()) as db:
            db.executescript(SCHEMA)

    def _db(self):
        db = sqlite3.connect(str(self.path), timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def _run(self, sql, args=()):
        with closing(self._db()) as db, db:
            return db.execute(sql, args).fetchall()

    # ---------------------------------------------------------------------------------------------- batches
    def add(self, server, task_id, batch: dict, source="", keyword=""):
        payload = gzip.compress(json.dumps({"batch": batch, "source": source, "keyword": keyword}, ensure_ascii=False).encode("utf-8"))
        self._run("INSERT OR REPLACE INTO batches (client_batch_id, server, task_id, payload, records, created_at) VALUES (?,?,?,?,?,?)",
                  (batch["client_batch_id"], server, int(task_id), payload, len(batch.get("records") or []), iso(utcnow())))

    def pending(self, server):
        rows = self._run("SELECT * FROM batches WHERE server=? ORDER BY created_at, rowid", (server,))
        out = []
        for row in rows:
            data = json.loads(gzip.decompress(row["payload"]).decode("utf-8"))
            out.append({"client_batch_id": row["client_batch_id"], "task_id": row["task_id"], "batch": data["batch"], "source": data.get("source", ""),
                        "keyword": data.get("keyword", ""), "records": row["records"], "created_at": row["created_at"], "batch_id": row["batch_id"],
                        "attempts": row["attempts"], "last_error": row["last_error"]})
        return out

    def mark_sent(self, client_batch_id, batch_id):
        self._run("UPDATE batches SET batch_id=?, attempts=attempts+1, last_error='' WHERE client_batch_id=?", (batch_id, client_batch_id))

    def mark_failed(self, client_batch_id, error):
        self._run("UPDATE batches SET attempts=attempts+1, last_error=? WHERE client_batch_id=?", (str(error)[:300], client_batch_id))

    def remove(self, client_batch_id):
        self._run("DELETE FROM batches WHERE client_batch_id=?", (client_batch_id,))

    def clear(self, server):
        rows = self._run("SELECT COUNT(*) AS n FROM batches WHERE server=?", (server,))
        self._run("DELETE FROM batches WHERE server=?", (server,))
        return rows[0]["n"]

    # ---------------------------------------------------------------------------------------------- history
    def record(self, server, *, task_id=None, source="", keyword="", batch_id="", status, reason="", received=0, new=0, completed=0, points=0):
        self._run("INSERT INTO history (at, server, task_id, source, keyword, batch_id, status, reason, received, new, completed, points) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (iso(utcnow()), server, task_id, source, keyword, batch_id, status, reason, received, new, completed, points))
        self._run("DELETE FROM history WHERE id NOT IN (SELECT id FROM history ORDER BY id DESC LIMIT ?)", (KEEP_HISTORY,))

    def history(self, server, limit=20):
        rows = self._run("SELECT * FROM history WHERE server=? ORDER BY id DESC LIMIT ?", (server, int(limit)))
        return [dict(r) for r in rows]

    def totals(self, server):
        row = self._run("SELECT COUNT(*) AS batches, COALESCE(SUM(new),0) AS new, COALESCE(SUM(completed),0) AS completed, COALESCE(SUM(points),0) AS points, "
                        "COALESCE(SUM(status='rejected'),0) AS rejected FROM history WHERE server=?", (server,))[0]
        return dict(row)
