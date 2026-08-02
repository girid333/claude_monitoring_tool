"use client";

/* Reports — generated Word/PDF artifacts, split by kind and grouped by date
   (D-035/D-036).

   Core BASIS sweeps and recorded flows are different documents with
   different templates, so they get their own tabs rather than one mixed
   list. Within a tab, reports are grouped by day (newest first) and can be
   filtered by date or free text — a landscape with hundreds of runs is
   unusable as a flat list. */

import { useCallback, useEffect, useMemo, useState } from "react";
import { apiBase } from "../lib/api";

const API = apiBase();

type ReportRow = {
  run_id: string; sid: string; label?: string; kind?: string;
  docx_path: string | null; pdf_path: string | null; created_at: string;
};

const dayOf = (ts: string) => (ts || "").slice(0, 10);      // "YYYY-MM-DD"

function prettyDay(day: string): string {
  if (!day) return "Unknown date";
  const today = new Date().toISOString().slice(0, 10);
  const yest = new Date(Date.now() - 864e5).toISOString().slice(0, 10);
  if (day === today) return `Today · ${day}`;
  if (day === yest) return `Yesterday · ${day}`;
  return day;
}

export default function Reports() {
  const [reports, setReports] = useState<ReportRow[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [confirming, setConfirming] = useState<string | null>(null);
  const [tab, setTab] = useState<"core_basis" | "flow">("core_basis");
  const [dateFilter, setDateFilter] = useState("");
  const [query, setQuery] = useState("");

  const load = useCallback(() => {
    fetch(`${API}/api/reports?limit=200`).then(r => r.json()).then(setReports)
      .catch(err => setError(`Could not reach the backend at ${API}: ${err instanceof Error ? err.message : String(err)}`));
  }, []);

  useEffect(load, [load]);

  const counts = useMemo(() => ({
    core_basis: reports.filter(r => (r.kind ?? "core_basis") === "core_basis").length,
    flow: reports.filter(r => r.kind === "flow").length,
  }), [reports]);

  // Filter by tab, then date, then free text; group the survivors by day.
  const groups = useMemo(() => {
    const q = query.trim().toLowerCase();
    const rows = reports.filter(r => {
      if ((r.kind ?? "core_basis") !== tab) return false;
      if (dateFilter && dayOf(r.created_at) !== dateFilter) return false;
      if (q && !(`${r.label ?? ""} ${r.sid} ${r.run_id}`.toLowerCase().includes(q))) return false;
      return true;
    });
    const byDay = new Map<string, ReportRow[]>();
    for (const r of rows) {
      const d = dayOf(r.created_at);
      if (!byDay.has(d)) byDay.set(d, []);
      byDay.get(d)!.push(r);
    }
    return [...byDay.entries()].sort((a, b) => b[0].localeCompare(a[0]));
  }, [reports, tab, dateFilter, query]);

  const shown = groups.reduce((n, [, rows]) => n + rows.length, 0);

  const remove = async (r: ReportRow) => {
    const key = `${r.run_id}-${r.sid}-${r.label ?? ""}`;
    setBusy(key);
    setError("");
    try {
      const res = await fetch(
        `${API}/api/reports/${r.run_id}/${r.sid}?label=${encodeURIComponent(r.label ?? "")}`,
        { method: "DELETE" });
      if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
      setReports(prev => prev.filter(x => `${x.run_id}-${x.sid}-${x.label ?? ""}` !== key));
    } catch (err) {
      setError(`Could not delete this report: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setBusy(null);
      setConfirming(null);
    }
  };

  return (
    <div style={{ maxWidth: 940 }} className="page-enter">
      <h1 className="ltitle">Reports</h1>
      <p className="lsub">
        <b>{counts.core_basis} Core BASIS</b> · <b>{counts.flow} functional flow</b> · Word + PDF
      </p>

      {error && (
        <div style={{ margin: "0 0 16px", padding: "10px 14px", borderRadius: 10,
                       border: "1px solid var(--red)", color: "var(--red)", fontSize: 12.5 }}>
          {error}
        </div>
      )}

      <div style={{ display: "flex", gap: 8, marginBottom: 14 }}>
        <button className={`btn ${tab === "core_basis" ? "" : "secondary"}`}
                onClick={() => setTab("core_basis")}>Core BASIS ({counts.core_basis})</button>
        <button className={`btn ${tab === "flow" ? "" : "secondary"}`}
                onClick={() => setTab("flow")}>Functional flows ({counts.flow})</button>
      </div>

      <div style={{ display: "flex", gap: 8, marginBottom: 16, alignItems: "center", flexWrap: "wrap" }}>
        <label style={{ fontSize: 12, color: "var(--text-2)" }}>Date</label>
        <input className="input" type="date" value={dateFilter}
               onChange={e => setDateFilter(e.target.value)} style={{ width: 165 }} />
        <input className="input" value={query} onChange={e => setQuery(e.target.value)}
               placeholder={tab === "flow" ? "Search by flow name, system, run…" : "Search by system or run…"}
               style={{ flex: "1 1 240px", minWidth: 180 }} />
        {(dateFilter || query) && (
          <button className="btn secondary" style={{ fontSize: 11.5, padding: "5px 10px" }}
                  onClick={() => { setDateFilter(""); setQuery(""); }}>Clear filters</button>
        )}
        <span style={{ fontSize: 11.5, color: "var(--text-3)" }}>{shown} shown</span>
      </div>

      {groups.length === 0 && (
        <div className="list"><div className="row"><span className="rsub">
          {reports.length === 0
            ? "No reports yet — one is generated automatically at the end of every run."
            : `No ${tab === "flow" ? "functional flow" : "Core BASIS"} reports match these filters.`}
        </span></div></div>
      )}

      {groups.map(([day, rows], gi) => (
        <div key={day} style={{ marginBottom: 22 }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: "var(--text-2)",
                         margin: "0 0 8px 2px" }}>
            {prettyDay(day)} <span style={{ color: "var(--text-3)", fontWeight: 400 }}>· {rows.length}</span>
          </div>
          <div className="list">
            {rows.map((r, i) => {
              const key = `${r.run_id}-${r.sid}-${r.label ?? ""}`;
              const qs = `?label=${encodeURIComponent(r.label ?? "")}`;
              const isFlow = r.kind === "flow";
              return (
                <div className="row rise" key={key}
                     style={{ ["--i" as string]: Math.min(gi * 2 + i, 10) }}>
                  <span className="rdot" style={{ background: isFlow ? "var(--green)" : "var(--blue)" }} />
                  <div className="grow">
                    <div className="rtitle">{r.label || "Core BASIS"}</div>
                    <div className="rsub">{r.sid} · {r.created_at} · run {r.run_id}</div>
                  </div>
                  <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                    {r.docx_path && (
                      <a className="btn secondary" href={`${API}/api/reports/${r.run_id}/${r.sid}/download/docx${qs}`}
                         style={{ textDecoration: "none" }}>Word</a>
                    )}
                    {r.pdf_path && (
                      <a className="btn secondary" href={`${API}/api/reports/${r.run_id}/${r.sid}/download/pdf${qs}`}
                         style={{ textDecoration: "none" }}>PDF</a>
                    )}
                    <a className="btn secondary" href={`${API}/api/reports/${r.run_id}/download/zip`}
                       title="Every report from this run, zipped" style={{ textDecoration: "none" }}>ZIP</a>
                    {confirming === key ? (
                      <>
                        <button className="btn danger" disabled={busy === key} onClick={() => remove(r)}>
                          {busy === key ? "Deleting…" : "Confirm"}
                        </button>
                        <button className="btn secondary" onClick={() => setConfirming(null)}>Cancel</button>
                      </>
                    ) : (
                      <button className="btn secondary" title="Delete this report (Word + PDF files too)"
                              onClick={() => setConfirming(key)}
                              style={{ color: "var(--red)" }}>Delete</button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      ))}
    </div>
  );
}
