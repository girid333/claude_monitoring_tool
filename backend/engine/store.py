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

-- D-017: CoreBasisSection results don't fit FlowRunResult's schema (v1's
-- results dict is raw, keyed by tcode, not by flow/step/check) so it gets
-- its own table rather than being force-fit into flow_runs.
CREATE TABLE IF NOT EXISTS core_basis_runs (
    run_id TEXT NOT NULL,
    sid TEXT NOT NULL,
    ok INTEGER NOT NULL,
    anomaly_count INTEGER NOT NULL,
    notes TEXT,
    started_at TEXT DEFAULT (datetime('now')),
    payload TEXT NOT NULL,
    PRIMARY KEY (run_id, sid)
);
CREATE INDEX IF NOT EXISTS idx_core_basis_sid ON core_basis_runs (sid, started_at DESC);

-- D-014: generated Word/PDF report artifacts per run, for the Reports UI.
CREATE TABLE IF NOT EXISTS report_runs (
    run_id TEXT NOT NULL,
    sid TEXT NOT NULL,
    docx_path TEXT,
    pdf_path TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (run_id, sid)
);
CREATE INDEX IF NOT EXISTS idx_reports_sid ON report_runs (sid, created_at DESC);
"""


class RunStore:
    def __init__(self, path: str | Path = "runs.db"):
        # check_same_thread=False: FastAPI's sync `def` endpoints run in a
        # threadpool (a different worker thread per call), but this
        # connection is created once at module load in the main thread.
        # Found live 2026-08-01 testing /api/reports — sqlite3 raised
        # "objects created in a thread can only be used in that same
        # thread" (same latent bug existed in the original /api/runs too,
        # just never exercised). A single sqlite3.Connection isn't safe for
        # true concurrent access, but FastAPI's threadpool serializes enough
        # for this dev tool's traffic; a connection-per-request pool is the
        # correct fix if this ever needs real concurrency.
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
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
        # started_at joined in alongside payload (was payload-only before —
        # the Runs page needs a timestamp per row and FlowRunResult itself
        # carries none, D-010 keys everything by run_id+sid not by time).
        q = ("SELECT payload, started_at FROM flow_runs "
             + ("WHERE sid = ? " if sid else "")
             + "ORDER BY started_at DESC LIMIT ?")
        args = (sid, limit) if sid else (limit,)
        out = []
        for payload, started_at in self.conn.execute(q, args):
            row = json.loads(payload)
            row["started_at"] = started_at
            out.append(row)
        return out

    def save_core_basis(self, run_id: str, sid: str, ok: bool,
                         anomaly_count: int, notes: list[str],
                         payload: dict) -> None:
        """D-017: stores CoreBasisSection's raw v1 results dict. Takes plain
        values rather than a SectionResult object so this module never needs
        to import engine.sections (which pulls in engine.py -> playwright at
        module scope) — the store has to stay importable standalone."""
        self.conn.execute(
            "INSERT OR REPLACE INTO core_basis_runs "
            "(run_id, sid, ok, anomaly_count, notes, payload) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, sid, int(ok), anomaly_count, json.dumps(notes),
             json.dumps(payload)))
        self.conn.commit()

    def recent_core_basis(self, sid: str | None = None, limit: int = 20) -> list[dict]:
        q = ("SELECT run_id, sid, ok, anomaly_count, notes, started_at, payload "
             "FROM core_basis_runs "
             + ("WHERE sid = ? " if sid else "")
             + "ORDER BY started_at DESC LIMIT ?")
        args = (sid, limit) if sid else (limit,)
        cols = ["run_id", "sid", "ok", "anomaly_count", "notes", "started_at", "payload"]
        rows = []
        for r in self.conn.execute(q, args):
            row = dict(zip(cols, r))
            row["ok"] = bool(row["ok"])
            row["notes"] = json.loads(row["notes"]) if row["notes"] else []
            row["payload"] = json.loads(row["payload"])
            rows.append(row)
        return rows

    def save_report(self, run_id: str, sid: str, docx_path: str | None,
                     pdf_path: str | None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO report_runs (run_id, sid, docx_path, pdf_path) "
            "VALUES (?, ?, ?, ?)", (run_id, sid, docx_path, pdf_path))
        self.conn.commit()

    def recent_reports(self, sid: str | None = None, limit: int = 20) -> list[dict]:
        q = ("SELECT run_id, sid, docx_path, pdf_path, created_at FROM report_runs "
             + ("WHERE sid = ? " if sid else "")
             + "ORDER BY created_at DESC LIMIT ?")
        args = (sid, limit) if sid else (limit,)
        cols = ["run_id", "sid", "docx_path", "pdf_path", "created_at"]
        return [dict(zip(cols, r)) for r in self.conn.execute(q, args)]
