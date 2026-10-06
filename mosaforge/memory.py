"""Private memory + experience log in one local SQLite file (FTS5 for retrieval).

Scopes (see ARCHITECTURE.md §Memory):
  working    — not stored here; it is the live message list of a run
  project    — architecture, conventions, decisions for a workspace
  long_term  — stable user preferences and reusable knowledge
  skill      — procedures that repeatedly worked
  experience — one row per run in `runs` (the evaluation table), plus distilled lessons here
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .core import TaskRecord

SCOPES = ("project", "long_term", "skill", "experience")

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY, scope TEXT NOT NULL, key TEXT, content TEXT NOT NULL,
  source TEXT, workspace TEXT, created REAL NOT NULL, uses INTEGER DEFAULT 0
);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(content, key, content='memories', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
  INSERT INTO memories_fts(rowid, content, key) VALUES (new.id, new.content, new.key);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, content, key) VALUES ('delete', old.id, old.content, old.key);
END;
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT, started_at REAL, task TEXT, category TEXT, agent TEXT, model TEXT, provider TEXT, node TEXT,
  privacy TEXT, route_confidence REAL, route_reasons TEXT, attempt INTEGER, escalated_from TEXT,
  latency_s REAL, prompt_tokens INTEGER, completion_tokens INTEGER, tokens_per_s REAL, tool_calls INTEGER,
  verified TEXT, verify_detail TEXT, success INTEGER, human_correction INTEGER, cost_usd REAL,
  load_s REAL, network_s REAL, bytes_lan INTEGER, error TEXT, answer TEXT
);
CREATE INDEX IF NOT EXISTS runs_cat_model ON runs(category, model);
"""


def _fts_query(text: str) -> str:
    words = [w for w in re.findall(r"[\w-]{3,}", text.lower())][:12]
    return " OR ".join(f'"{w}"' for w in words)


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(runs)")}
        for col, typ in (("load_s", "REAL"), ("network_s", "REAL"), ("bytes_lan", "INTEGER")):  # migrate pre-0.2 DBs
            if col not in cols:
                self.db.execute(f"ALTER TABLE runs ADD COLUMN {col} {typ}")
        self.lock = threading.RLock()

    # ---- memory -------------------------------------------------------
    def add(self, scope: str, content: str, key: str = "", source: str = "", workspace: str = "") -> int:
        if scope not in SCOPES:
            raise ValueError(f"scope must be one of {SCOPES}")
        cur = self.db.execute(
            "INSERT INTO memories(scope, key, content, source, workspace, created) VALUES (?,?,?,?,?,?)",
            (scope, key, content, source, workspace, time.time()),
        )
        self.db.commit()
        return int(cur.lastrowid or 0)

    def search(self, query: str, scopes: tuple[str, ...] = SCOPES, limit: int = 5, workspace: str | None = None) -> list[dict[str, Any]]:
        q = _fts_query(query)
        if not q:
            return []
        marks = ",".join("?" * len(scopes))
        sql = (f"SELECT m.id, m.scope, m.key, m.content, m.workspace FROM memories_fts f JOIN memories m ON m.id=f.rowid "
               f"WHERE memories_fts MATCH ? AND m.scope IN ({marks})")
        params: list[Any] = [q, *scopes]
        if workspace is not None:
            sql += " AND (m.workspace = ? OR m.workspace = '' OR m.scope = 'long_term')"
            params.append(workspace)
        sql += " ORDER BY bm25(memories_fts) LIMIT ?"
        params.append(limit)
        rows = [dict(r) for r in self.db.execute(sql, params)]
        if rows:
            self.db.execute(f"UPDATE memories SET uses = uses + 1 WHERE id IN ({','.join('?' * len(rows))})", [r["id"] for r in rows])
            self.db.commit()
        return rows

    def forget(self, mem_id: int) -> None:
        self.db.execute("DELETE FROM memories WHERE id = ?", (mem_id,))
        self.db.commit()

    def list(self, scope: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if scope:
            cur = self.db.execute("SELECT * FROM memories WHERE scope=? ORDER BY id DESC LIMIT ?", (scope, limit))
        else:
            cur = self.db.execute("SELECT * FROM memories ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in cur]

    # ---- experience / evaluation --------------------------------------
    def record(self, rec: TaskRecord) -> None:
        d = asdict(rec)
        cols = ",".join(d)
        with self.lock:
            self.db.execute(f"INSERT INTO runs({cols}) VALUES ({','.join('?' * len(d))})", list(d.values()))
            self.db.commit()

    def mark(self, run_id: str, success: int, human_correction: int | None = None) -> int:
        with self.lock:
            cur = self.db.execute("UPDATE runs SET success=?, human_correction=COALESCE(?, human_correction) WHERE run_id=?",
                                  (success, human_correction, run_id))
            self.db.commit()
        return cur.rowcount

    def pass_rate(self, category: str, model: str) -> tuple[int, float]:
        r = self.db.execute(
            "SELECT COUNT(*) n, AVG(CASE WHEN COALESCE(success, verified='pass') THEN 1.0 ELSE 0.0 END) p "
            "FROM runs WHERE category=? AND model=? AND verified != 'unknown'", (category, model)).fetchone()
        return int(r["n"] or 0), float(r["p"] or 0.0)

    def stats(self) -> list[dict[str, Any]]:
        sql = """SELECT category, model, node, COUNT(*) runs,
                 ROUND(AVG(verified='pass'),2) pass_rate, ROUND(AVG(latency_s),1) avg_latency_s,
                 ROUND(AVG(tokens_per_s),1) avg_tok_s, SUM(prompt_tokens+completion_tokens) tokens,
                 ROUND(SUM(cost_usd),4) cost_usd
                 FROM runs GROUP BY category, model, node ORDER BY category, runs DESC"""
        return [dict(r) for r in self.db.execute(sql)]

    def export_jsonl(self, path: Path) -> int:
        n = 0
        with open(path, "w") as f:
            for r in self.db.execute("SELECT * FROM runs ORDER BY started_at"):
                f.write(json.dumps(dict(r)) + "\n")
                n += 1
        return n
