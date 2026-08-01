"""Run history store (Phase 6). SQLite, stateless writes keyed run_id+sid
(D-010). Enables the UI's recent-runs list and drift analysis over time."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .schema import FlowRunResult

_SCHEMA = """
CREATE TABLE IF NOT EXISTS flow_runs (
    run_id TEXT NOT NULL,
    sid TEXT NOT NULL,
    flow_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    ok INTEGER NOT NULL,
    error TEXT,
    started_at TEXT DEFAULT (datetime('now')),
    payload TEXT NOT NULL,
    PRIMARY KEY (run_id, sid, flow_id)
);
CREATE INDEX IF NOT EXISTS idx_runs_sid ON flow_runs (sid, started_at DESC);
"""


class RunStore:
    def __init__(self, path: str | Path = "runs.db"):
        self.conn = sqlite3.connect(str(path))
        self.conn.executescript(_SCHEMA)

    def save(self, result: FlowRunResult) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO flow_runs "
            "(run_id, sid, flow_id, mode, ok, error, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (result.run_id, result.sid, result.flow_id, result.mode,
             int(result.ok), result.error, result.model_dump_json()))
        self.conn.commit()

    def recent(self, sid: str | None = None, limit: int = 20) -> list[dict]:
        q = ("SELECT payload FROM flow_runs "
             + ("WHERE sid = ? " if sid else "")
             + "ORDER BY started_at DESC LIMIT ?")
        args = (sid, limit) if sid else (limit,)
        return [json.loads(r[0]) for r in self.conn.execute(q, args)]
