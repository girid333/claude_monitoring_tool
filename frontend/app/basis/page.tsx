"use client";

/* Landscape — THE place to start a Core BASIS run (D-031; moved from "/" to
   "/basis" in D-050, which also moved every record/replay control to
   /replay — this page runs Core BASIS only).

   Runs are launched only from here (the Runs page is history-only) so there
   is one obvious execution path. Live progress — per-T-code checklist, phase
   text, errors — renders here and, because run state lives in RunContext at
   the layout level, survives navigating to any other tab and back. */

import { useEffect, useState } from "react";
import { apiBase } from "../lib/api";
import { useRun } from "../components/RunContext";

const API = apiBase();

type SystemRow = {
  sid: string; system_name?: string; release_tag: string; auth: string; tags: string[];
  last_run: { ok: boolean; flow_id: string; checks: { ok: boolean; severity: string }[] } | null;
  credentials?: { sap_credentials: boolean };
};

function healthOf(s: SystemRow): "ok" | "warn" | "crit" {
  if (!s.last_run) return "ok";
  const failed = s.last_run.checks?.filter(c => !c.ok) ?? [];
  if (failed.some(c => ["high", "critical"].includes(c.severity))) return "crit";
  if (failed.length > 0) return "warn";
  return "ok";
}

const DOT = { ok: "var(--green)", warn: "var(--orange)", crit: "var(--red)" };

export default function Landscape() {
  const [systems, setSystems] = useState<SystemRow[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  // D-036: Core BASIS is OFF by default — a 23-T-code sweep takes minutes,
  // so it should be a deliberate choice, not something you opt out of.
  const [coreBasis, setCoreBasis] = useState(false);
  const [loadError, setLoadError] = useState("");
  const run = useRun();

  useEffect(() => {
    fetch(`${API}/api/systems`).then(r => r.json()).then(setSystems)
      .catch(err => setLoadError(`Could not reach the backend at ${API}: ${err instanceof Error ? err.message : String(err)}`));
  }, [run.done]);

  const canRun = selected.size > 0 && coreBasis && !run.isRunning;

  const doneCount = run.tcodes.filter(t => ["done", "warn"].includes(run.tcodeStatus[t])).length;
  const pct = run.tcodes.length ? Math.round((doneCount / run.tcodes.length) * 100) : 0;

  return (
    <div style={{ maxWidth: 1100 }} className="page-enter">
      <h1 className="ltitle">Landscape</h1>
      <p className="lsub">
        <b style={{ color: "var(--text)" }}>{systems.length} systems</b>
        {" · select one or more, tick Core BASIS sweep, then Execute"}
      </p>

      {loadError && (
        <div style={{ margin: "0 0 16px", padding: "10px 14px", borderRadius: 10,
                       border: "1px solid var(--red)", color: "var(--red)", fontSize: 12.5 }}>
          {loadError}
        </div>
      )}

      {/* ── Execution bar ── */}
      <div className="panel" style={{ padding: "14px 18px", marginBottom: 22, borderRadius: 14,
                                       display: "flex", alignItems: "center", gap: 18, flexWrap: "wrap" }}>
        <div style={{ fontSize: 13, fontWeight: 600 }}>
          {selected.size === 0 ? "Select a system below" : `${selected.size} selected`}
        </div>
        <label style={{ fontSize: 12.5, color: "var(--text-2)", cursor: "pointer" }}>
          <input type="checkbox" checked={coreBasis} onChange={e => setCoreBasis(e.target.checked)}
                 style={{ marginRight: 6, verticalAlign: -1 }} />
          Core BASIS sweep <span style={{ color: "var(--text-3)" }}>(23 T-codes + SSH + URL checks → Word/PDF report)</span>
        </label>
        <button className="btn" disabled={!canRun}
                onClick={() => run.startRun([...selected], { coreBasis, flows: [] })}
                style={{ marginLeft: "auto" }}>
          {run.isRunning ? "Running…" : "Execute"}
        </button>
      </div>

      {/* D-050: record/replay moved to /replay — Landscape runs Core BASIS only. */}
      <div className="rsub" style={{ margin: "-8px 0 22px", fontSize: 12 }}>
        Replaying recorded transactions and data runs now live in{" "}
        <a href="/replay" style={{ color: "var(--blue)" }}>Record and replay</a>.
      </div>

      {/* ── Live progress (survives navigation via RunContext) ── */}
      {(run.isRunning || run.done) && (run.tcodes.length > 0 || run.phase || run.errors.length > 0) && (
        <div className="panel island" style={{ padding: "16px 18px", marginBottom: 22, borderRadius: 16 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 10 }}>
            <span className={run.isRunning ? "pulse" : ""} style={{
              width: 9, height: 9, borderRadius: "50%",
              background: run.errors.length ? "var(--red)" : run.isRunning ? "var(--blue)" : "var(--green)",
            }} />
            <div style={{ fontSize: 13, fontWeight: 600 }}>
              {run.isRunning ? "Run in progress" : run.errors.length ? "Run finished with errors" : "Run complete"}
            </div>
            {run.tcodes.length > 0 && (
              <div style={{ fontSize: 11.5, color: "var(--text-3)" }}>
                {doneCount}/{run.tcodes.length} T-codes
              </div>
            )}
            {run.done && (
              <button className="btn secondary" style={{ marginLeft: "auto", fontSize: 11.5, padding: "4px 10px" }}
                      onClick={run.dismiss}>Dismiss</button>
            )}
          </div>

          {run.phase && (
            <div style={{ fontSize: 12.5, color: "var(--text-2)", marginBottom: 10 }}>{run.phase}</div>
          )}

          {run.tcodes.length > 0 && (
            <>
              <div className="pbar" style={{ marginBottom: 12 }}><span style={{ width: `${pct}%` }} /></div>
              <div className="tgrid">
                {run.tcodes.map(t => {
                  const st = run.tcodeStatus[t] ?? "pending";
                  return (
                    <div key={t} className={`tchip ${st === "pending" ? "" : st}`}>
                      <span className="tdot" />{t}
                    </div>
                  );
                })}
              </div>
            </>
          )}

          {run.errors.length > 0 && (
            <div style={{ marginTop: 12, padding: "10px 12px", borderRadius: 10,
                           border: "1px solid var(--red)", color: "var(--red)", fontSize: 12.5 }}>
              {run.errors.map((e, i) => <div key={i} style={{ marginTop: i ? 4 : 0 }}>{e}</div>)}
            </div>
          )}

          {run.done && !run.errors.length && (
            <div style={{ marginTop: 12, fontSize: 12.5, color: "var(--green)" }}>
              ✓ Finished — Word and PDF reports are on the <a href="/reports" style={{ color: "var(--blue)" }}>Reports</a> page.
            </div>
          )}
        </div>
      )}

      {/* ── System cards (tvOS focus behaviour) ── */}
      <div style={{ display: "grid", gap: 16, gridTemplateColumns: "repeat(auto-fill,minmax(230px,1fr))" }}>
        {systems.length === 0 && !loadError && (
          <div className="rsub">No systems yet — add one on the Systems page.</div>
        )}
        {systems.map((s, i) => {
          const h = healthOf(s);
          const on = selected.has(s.sid);
          const active = run.sid === s.sid && run.isRunning;
          return (
            <div key={s.sid} className="panel card rise" tabIndex={0}
              onClick={() => setSelected(prev => {
                const n = new Set(prev); n.has(s.sid) ? n.delete(s.sid) : n.add(s.sid); return n;
              })}
              onKeyDown={e => { if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                setSelected(prev => { const n = new Set(prev); n.has(s.sid) ? n.delete(s.sid) : n.add(s.sid); return n; });
              }}}
              style={{ padding: "18px 18px 16px",
                        outline: on ? "2px solid var(--blue)" : "none",
                        ["--i" as string]: i }}>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <div style={{ minWidth: 0 }}>
                  <div style={{ fontSize: 22, fontWeight: 700 }}>{s.sid}</div>
                  <div style={{ fontSize: 11.5, color: "var(--text-3)", marginTop: 2,
                                 overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {s.system_name ? `${s.system_name} · ` : ""}{s.release_tag} · {s.auth.toUpperCase()}
                  </div>
                </div>
                <span className={active ? "pulse" : ""} style={{
                  width: 11, height: 11, borderRadius: "50%", marginTop: 5, flexShrink: 0,
                  background: active ? "var(--blue)" : DOT[h],
                  boxShadow: `0 0 10px ${active ? "var(--blue)" : DOT[h]}`,
                }} />
              </div>
              <div style={{ marginTop: 14, paddingTop: 12, borderTop: "1px solid var(--hairline)",
                             fontSize: 12, color: "var(--text-2)" }}>
                {active ? "running now…"
                  : s.last_run ? `${s.last_run.flow_id} · ${s.last_run.ok ? "clean" : "anomalies"}`
                  : "no runs yet"}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
