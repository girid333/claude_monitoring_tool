"use client";

/* Landscape — THE place to start a run (D-031).

   Runs are launched only from here (the Runs page is history-only) so there
   is one obvious execution path. Live progress — per-T-code checklist, phase
   text, errors — renders here and, because run state lives in RunContext at
   the layout level, survives navigating to any other tab and back. */

import { useEffect, useState } from "react";
import { apiBase } from "./lib/api";
import { useRun } from "./components/RunContext";

const API = apiBase();

type FlowInfo = {
  flow_id: string; description: string; step_count: number;
  tcodes: string[]; has_capture: boolean;
};
type FlowStep = { act: string; target: string; value: unknown; resolved_by: string };
type FlowDetail = {
  flow_id: string; description: string; step_count: number; steps: FlowStep[];
};
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
  // D-034/D-036: pick WHICH recorded flows to replay, with search + bulk select
  const [flowLib, setFlowLib] = useState<FlowInfo[]>([]);
  const [pickedFlows, setPickedFlows] = useState<Set<string>>(new Set());
  const [flowQuery, setFlowQuery] = useState("");
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  // D-041: "what did this recording actually capture?" — Giri asked to see
  // the recorded content directly rather than inferring it from a replayed
  // report, so a step can be pinpointed as missing vs. just misresolving.
  const [viewingFlow, setViewingFlow] = useState<string | null>(null);
  const [flowDetail, setFlowDetail] = useState<FlowDetail | null>(null);
  const [flowDetailError, setFlowDetailError] = useState("");
  // D-043: replay-time input edits — {flowId: {stepIndex: newValue}}.
  // Edited in the step viewer, sent with Execute, applied in memory only
  // (the saved recording is never modified).
  const [overrides, setOverrides] = useState<Record<string, Record<string, string>>>({});

  const run = useRun();

  useEffect(() => {
    fetch(`${API}/api/systems`).then(r => r.json()).then(setSystems)
      .catch(err => setLoadError(`Could not reach the backend at ${API}: ${err instanceof Error ? err.message : String(err)}`));
    fetch(`${API}/api/flows`).then(r => r.json()).then(setFlowLib).catch(() => {});
  }, [run.done]);

  const canRun = selected.size > 0 && (coreBasis || pickedFlows.size > 0) && !run.isRunning;

  // D-036: filter the flow library — there can be hundreds of recordings.
  const q = flowQuery.trim().toLowerCase();
  const visibleFlows = q
    ? flowLib.filter(f =>
        f.flow_id.toLowerCase().includes(q)
        || (f.description || "").toLowerCase().includes(q)
        || f.tcodes.some(t => (t || "").toLowerCase().includes(q)))
    : flowLib;

  const deleteFlow = async (flowId: string) => {
    try {
      const res = await fetch(`${API}/api/flows/${encodeURIComponent(flowId)}`, { method: "DELETE" });
      if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
      setFlowLib(prev => prev.filter(f => f.flow_id !== flowId));
      setPickedFlows(prev => { const n = new Set(prev); n.delete(flowId); return n; });
      if (viewingFlow === flowId) { setViewingFlow(null); setFlowDetail(null); }
    } catch (err) {
      setLoadError(`Could not delete ${flowId}: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setConfirmDelete(null);
    }
  };

  // D-041: toggle the inline "what did this recording capture?" panel.
  const viewSteps = async (flowId: string) => {
    if (viewingFlow === flowId) { setViewingFlow(null); setFlowDetail(null); return; }
    setViewingFlow(flowId);
    setFlowDetail(null);
    setFlowDetailError("");
    try {
      const res = await fetch(`${API}/api/flows/${encodeURIComponent(flowId)}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
      setFlowDetail(await res.json());
    } catch (err) {
      setFlowDetailError(err instanceof Error ? err.message : String(err));
    }
  };

  const doneCount = run.tcodes.filter(t => ["done", "warn"].includes(run.tcodeStatus[t])).length;
  const pct = run.tcodes.length ? Math.round((doneCount / run.tcodes.length) * 100) : 0;

  return (
    <div style={{ maxWidth: 1100 }} className="page-enter">
      <h1 className="ltitle">Landscape</h1>
      <p className="lsub">
        <b style={{ color: "var(--text)" }}>{systems.length} systems</b>
        {" · select one or more, choose what to run, then Execute"}
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
                onClick={() => run.startRun([...selected], {
                  coreBasis, flows: [...pickedFlows],
                  flowOverrides: overrides,     // D-043: replay-time input edits
                })}
                style={{ marginLeft: "auto" }}>
          {run.isRunning ? "Running…" : "Execute"}
        </button>
      </div>

      {/* ── D-034: choose which recorded flows to replay ── */}
      <div className="panel" style={{ padding: "14px 18px", marginBottom: 22, borderRadius: 14 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 10, flexWrap: "wrap" }}>
          <div style={{ fontSize: 13, fontWeight: 600 }}>Functional flows (recordings)</div>
          <div style={{ fontSize: 11.5, color: "var(--text-3)" }}>
            {pickedFlows.size > 0 ? `${pickedFlows.size} selected` : "none selected"}
            {q && ` · ${visibleFlows.length} of ${flowLib.length} shown`}
          </div>
          <a href="/recorder" style={{ marginLeft: "auto", fontSize: 11.5, color: "var(--blue)" }}>
            + Record a new flow
          </a>
        </div>

        {flowLib.length === 0 ? (
          <div className="rsub">
            No recordings yet — use the <a href="/recorder" style={{ color: "var(--blue)" }}>Recorder</a> to
            capture a transaction, then it appears here.
          </div>
        ) : (
          <>
            {/* D-036: search + bulk select — the library grows to hundreds */}
            <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 10, flexWrap: "wrap" }}>
              <input className="input" value={flowQuery} onChange={e => setFlowQuery(e.target.value)}
                     placeholder="Search by name, T-code or description…"
                     style={{ flex: "1 1 260px", minWidth: 200 }} />
              <button className="btn secondary" style={{ fontSize: 11.5, padding: "5px 10px" }}
                      onClick={() => setPickedFlows(prev => {
                        const n = new Set(prev);
                        visibleFlows.forEach(f => n.add(f.flow_id));
                        return n;
                      })}>
                Select {q ? "filtered" : "all"}
              </button>
              <button className="btn secondary" style={{ fontSize: 11.5, padding: "5px 10px" }}
                      disabled={pickedFlows.size === 0}
                      onClick={() => setPickedFlows(new Set())}>Clear</button>
            </div>

            {visibleFlows.length === 0 ? (
              <div className="rsub">No recordings match &quot;{flowQuery}&quot;.</div>
            ) : (
              <div style={{ display: "flex", flexWrap: "wrap", gap: 8,
                             maxHeight: 220, overflowY: "auto" }}>
                {visibleFlows.map(f => {
                  const on = pickedFlows.has(f.flow_id);
                  const confirming = confirmDelete === f.flow_id;
                  return (
                    <span key={f.flow_id} className="btn secondary"
                          style={{ borderRadius: 999, fontSize: 12.5, display: "inline-flex",
                                    alignItems: "center", gap: 6, cursor: "default",
                                    outline: on ? "2px solid var(--blue)" : "none",
                                    color: on ? "var(--blue)" : undefined }}>
                      <span onClick={() => setPickedFlows(prev => {
                              const n = new Set(prev);
                              if (n.has(f.flow_id)) n.delete(f.flow_id); else n.add(f.flow_id);
                              return n;
                            })}
                            title={`${f.step_count} steps${f.tcodes.length ? ` · ${f.tcodes.join(", ")}` : ""}${f.has_capture ? " · has screenshots" : ""}`}
                            style={{ cursor: "pointer" }}>
                        {on ? "✓ " : ""}{f.flow_id}
                        <span style={{ color: "var(--text-3)", marginLeft: 6 }}>
                          {f.tcodes.length ? f.tcodes.join(",") : `${f.step_count} steps`}
                        </span>
                      </span>
                      {/* D-041: see exactly what this recording captured */}
                      <span onClick={() => viewSteps(f.flow_id)}
                            title="View recorded steps"
                            style={{ color: viewingFlow === f.flow_id ? "var(--blue)" : "var(--text-3)",
                                      cursor: "pointer", paddingLeft: 2 }}>👁</span>
                      {/* D-036: delete a recording */}
                      {confirming ? (
                        <>
                          <span onClick={() => deleteFlow(f.flow_id)}
                                style={{ color: "var(--red)", fontWeight: 700, cursor: "pointer" }}
                                title="Confirm delete">delete?</span>
                          <span onClick={() => setConfirmDelete(null)}
                                style={{ color: "var(--text-3)", cursor: "pointer" }}>✕</span>
                        </>
                      ) : (
                        <span onClick={() => setConfirmDelete(f.flow_id)}
                              title="Delete this recording"
                              style={{ color: "var(--text-3)", cursor: "pointer", paddingLeft: 2 }}>🗑</span>
                      )}
                    </span>
                  );
                })}
              </div>
            )}

            {/* D-041: inline "what did this recording actually capture?" panel */}
            {viewingFlow && (
              <div className="panel" style={{ marginTop: 12, padding: "12px 14px", borderRadius: 10 }}>
                <div style={{ display: "flex", alignItems: "center", marginBottom: 8, gap: 10 }}>
                  <div style={{ fontSize: 12.5, fontWeight: 600 }}>
                    Recorded steps — {viewingFlow}
                    {flowDetail && <span style={{ color: "var(--text-3)", fontWeight: 400 }}> · {flowDetail.step_count} steps</span>}
                  </div>
                  {/* D-043: replay-time edit status */}
                  {viewingFlow && Object.keys(overrides[viewingFlow] ?? {}).length > 0 && (
                    <>
                      <span style={{ fontSize: 11.5, color: "var(--orange)" }}>
                        {Object.keys(overrides[viewingFlow] ?? {}).length} value(s) changed for next run
                      </span>
                      <button className="btn secondary" style={{ fontSize: 11, padding: "3px 8px" }}
                              onClick={() => setOverrides(prev => {
                                const n = { ...prev }; delete n[viewingFlow!]; return n;
                              })}>Reset to recorded</button>
                    </>
                  )}
                  <span onClick={() => { setViewingFlow(null); setFlowDetail(null); }}
                        style={{ marginLeft: "auto", color: "var(--text-3)", cursor: "pointer", fontSize: 12.5 }}>close ✕</span>
                </div>
                {flowDetailError && (
                  <div style={{ fontSize: 12, color: "var(--red)" }}>Could not load: {flowDetailError}</div>
                )}
                {!flowDetail && !flowDetailError && (
                  <div className="rsub">Loading…</div>
                )}
                {flowDetail && (
                  <div style={{ maxHeight: 260, overflowY: "auto" }}>
                    <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
                      <thead>
                        <tr style={{ color: "var(--text-3)", textAlign: "left" }}>
                          <th style={{ padding: "3px 8px 3px 0", fontWeight: 500, width: 32 }}>#</th>
                          <th style={{ padding: "3px 8px", fontWeight: 500, width: 90 }}>action</th>
                          <th style={{ padding: "3px 8px", fontWeight: 500 }}>target</th>
                          <th style={{ padding: "3px 8px", fontWeight: 500 }}>value</th>
                          <th style={{ padding: "3px 0", fontWeight: 500, width: 80 }}>resolved by</th>
                        </tr>
                      </thead>
                      <tbody>
                        {flowDetail.steps.map((s, i) => {
                          const recorded = s.value === null || s.value === undefined ? "" : String(s.value);
                          // D-043: SET steps are editable — a recording's
                          // values are a template, not a contract. Toggle
                          // values (check/uncheck/select) stay read-only for
                          // now; free-text inputs are the real use case.
                          const editable = s.act === "set"
                            && !["check", "uncheck", "select"].includes(recorded);
                          const edited = overrides[viewingFlow!]?.[String(i)];
                          return (
                            <tr key={i} style={{ borderTop: "1px solid var(--hairline)" }}>
                              <td style={{ padding: "4px 8px 4px 0", color: "var(--text-3)" }}>{i}</td>
                              <td style={{ padding: "4px 8px", fontWeight: 600 }}>{s.act}</td>
                              <td style={{ padding: "4px 8px", wordBreak: "break-word" }}>{s.target}</td>
                              <td style={{ padding: "4px 8px", color: "var(--text-2)", wordBreak: "break-word" }}>
                                {editable ? (
                                  <input className="input"
                                         value={edited ?? recorded}
                                         title="Change this value for the next replay (the recording itself is not modified)"
                                         onChange={e => {
                                           const v = e.target.value;
                                           setOverrides(prev => {
                                             const flowOv = { ...(prev[viewingFlow!] ?? {}) };
                                             if (v === recorded) delete flowOv[String(i)];
                                             else flowOv[String(i)] = v;
                                             const n = { ...prev };
                                             if (Object.keys(flowOv).length) n[viewingFlow!] = flowOv;
                                             else delete n[viewingFlow!];
                                             return n;
                                           });
                                         }}
                                         style={{ width: "100%", fontSize: 12, padding: "3px 8px",
                                                   ...(edited !== undefined
                                                       ? { borderColor: "var(--orange)", color: "var(--orange)" }
                                                       : {}) }} />
                                ) : recorded}
                              </td>
                              <td style={{ padding: "4px 0", color: "var(--text-3)" }}>{s.resolved_by}</td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            )}
          </>
        )}
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
