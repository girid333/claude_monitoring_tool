"use client";

/* Runs — launch new runs AND browse history (D-029).

   Top section = v1's Batch Execution UX: pick one or more systems, pick
   which functions to run (Core BASIS sweep / functional flows), Execute,
   watch live status via SSE, see a clear success/error outcome. History
   lists below auto-refresh when a run finishes.

   (Before D-029 this page was history-only and the two tabs looked like
   action buttons that "did nothing" — the launcher only existed on
   Landscape.) */

import { useCallback, useEffect, useRef, useState } from "react";
import { apiBase } from "../lib/api";

const API = apiBase();

type FlowRun = {
  run_id: string; sid: string; flow_id: string; mode: string; ok: boolean;
  error: string | null; started_at?: string;
  checks: { ok: boolean; severity: string; message: string }[];
};
type CoreBasisRun = {
  run_id: string; sid: string; ok: boolean; anomaly_count: number;
  notes: string[]; started_at: string;
};
type SystemRow = { sid: string; system_name?: string };
type RunEvent = { type: string; sid?: string; flow_id?: string; ok?: boolean; error?: string; anomaly_count?: number };

function worstSeverity(checks: FlowRun["checks"]): string | null {
  const order = ["critical", "high", "medium", "low", "info"];
  const failed = checks.filter(c => !c.ok).map(c => c.severity);
  return order.find(s => failed.includes(s)) ?? null;
}

export default function Runs() {
  const [flowRuns, setFlowRuns] = useState<FlowRun[]>([]);
  const [coreBasisRuns, setCoreBasisRuns] = useState<CoreBasisRun[]>([]);
  const [tab, setTab] = useState<"flows" | "core_basis">("core_basis");

  // ── Launcher state (D-029) ──
  const [systems, setSystems] = useState<SystemRow[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [doCoreBasis, setDoCoreBasis] = useState(true);
  const [doFlows, setDoFlows] = useState(false);
  const [runId, setRunId] = useState<string | null>(null);
  const [statusLine, setStatusLine] = useState<string>("");
  const [runErrors, setRunErrors] = useState<string[]>([]);
  const [runDone, setRunDone] = useState(false);
  const [launchError, setLaunchError] = useState<string>("");
  const esRef = useRef<EventSource | null>(null);

  const loadHistory = useCallback(() => {
    fetch(`${API}/api/runs?limit=50`).then(r => r.json()).then(setFlowRuns).catch(() => {});
    fetch(`${API}/api/runs/core-basis?limit=50`).then(r => r.json()).then(setCoreBasisRuns).catch(() => {});
  }, []);

  useEffect(() => {
    loadHistory();
    fetch(`${API}/api/systems`).then(r => r.json()).then(setSystems)
      .catch(err => setLaunchError(`Could not reach the backend at ${API}: ${err instanceof Error ? err.message : String(err)}`));
    return () => { esRef.current?.close(); };
  }, [loadHistory]);

  const toggle = (sid: string) => setSelected(prev => {
    const n = new Set(prev);
    if (n.has(sid)) n.delete(sid); else n.add(sid);
    return n;
  });

  const execute = async () => {
    setLaunchError("");
    setRunErrors([]);
    setRunDone(false);
    setStatusLine("Starting run…");
    let rid: string;
    try {
      const res = await fetch(`${API}/api/run`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          sids: [...selected],
          flows: doFlows ? ["flows/sm37_cancelled_jobs.json"] : [],
          include_core_basis: doCoreBasis,
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
      rid = (await res.json()).run_id;
    } catch (err) {
      setStatusLine("");
      setLaunchError(`Could not start the run: ${err instanceof Error ? err.message : String(err)}`);
      return;
    }
    setRunId(rid);
    const es = new EventSource(`${API}/api/events/${rid}`);
    esRef.current = es;
    es.onmessage = e => {
      const ev: RunEvent = JSON.parse(e.data);
      if (ev.type === "system_start") setStatusLine(`Processing ${ev.sid}…`);
      if (ev.type === "probe") setStatusLine(`${ev.sid}: logged in, probing capabilities…`);
      if (ev.type === "flow_start") setStatusLine(`${ev.sid}: running flow ${ev.flow_id}…`);
      if (ev.type === "core_basis_start") setStatusLine(`${ev.sid}: Core BASIS sweep running (23 T-codes — this takes a while)…`);
      if (ev.type === "core_basis_done") setStatusLine(`${ev.sid}: Core BASIS done — ${ev.ok ? "clean" : `${ev.anomaly_count} anomalies`}`);
      if (ev.type === "report_start") setStatusLine(`${ev.sid}: generating Word/PDF report…`);
      if (ev.type === "report_ready") setStatusLine(`${ev.sid}: report ready`);
      if (ev.type === "system_error") setRunErrors(prev => [...prev, `${ev.sid}: ${ev.error ?? "unknown error"}`]);
      if (ev.type === "run_error") setRunErrors(prev => [...prev, `Run failed: ${ev.error ?? "unknown error"}`]);
      if (ev.type === "run_done") {
        es.close();
        esRef.current = null;
        setRunId(null);
        setRunDone(true);
        setStatusLine("");
        loadHistory();
      }
    };
    es.onerror = () => {
      // EventSource auto-reconnects on transient errors (readyState
      // CONNECTING) — only give up when the browser has closed it for good.
      if (es.readyState !== EventSource.CLOSED) return;
      esRef.current = null;
      setRunId(null);
      setRunDone(true);
      setRunErrors(prev => [...prev, "Lost connection to the run's event stream — check the Runs history below and the backend logs."]);
      setStatusLine("");
      loadHistory();
    };
  };

  const canExecute = selected.size > 0 && (doCoreBasis || doFlows) && !runId;

  return (
    <div style={{ maxWidth: 900 }}>
      <h1 className="ltitle">Runs</h1>
      <p className="lsub">
        <b>{flowRuns.length} flow runs</b> · <b>{coreBasisRuns.length} Core BASIS runs</b>
      </p>

      {/* ── New run launcher (D-029, v1 Batch Execution parity) ── */}
      <div className="panel" style={{ padding: "16px 18px", marginBottom: 22, borderRadius: 14 }}>
        <div style={{ fontSize: 13.5, fontWeight: 600, marginBottom: 10 }}>Start a run</div>

        {systems.length === 0 && (
          <div className="rsub" style={{ marginBottom: 8 }}>
            No systems in the registry yet — add one on the Systems page first.
          </div>
        )}

        <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginBottom: 12 }}>
          {systems.map(s => {
            const on = selected.has(s.sid);
            return (
              <button key={s.sid} onClick={() => toggle(s.sid)} className="btn secondary" style={{
                borderRadius: 999, fontSize: 12.5,
                outline: on ? "2px solid var(--blue)" : "none",
                color: on ? "var(--blue)" : undefined,
              }}>
                {on ? "✓ " : ""}{s.system_name || s.sid}{s.system_name ? ` (${s.sid})` : ""}
              </button>
            );
          })}
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 16, flexWrap: "wrap" }}>
          <label style={{ fontSize: 12.5, color: "var(--text-2)", cursor: "pointer" }}>
            <input type="checkbox" checked={doCoreBasis}
                   onChange={e => setDoCoreBasis(e.target.checked)}
                   style={{ marginRight: 6, verticalAlign: -1 }} />
            Core BASIS sweep (23 T-codes + SSH + URL checks)
          </label>
          <label style={{ fontSize: 12.5, color: "var(--text-2)", cursor: "pointer" }}>
            <input type="checkbox" checked={doFlows}
                   onChange={e => setDoFlows(e.target.checked)}
                   style={{ marginRight: 6, verticalAlign: -1 }} />
            Functional flows (recorded)
          </label>
          <button className="btn" disabled={!canExecute} onClick={execute}
                  style={{ marginLeft: "auto", opacity: canExecute ? 1 : 0.5 }}>
            {runId ? "Running…" : `Execute ${selected.size} system${selected.size === 1 ? "" : "s"}`}
          </button>
        </div>

        {statusLine && (
          <div style={{ marginTop: 12, fontSize: 12.5, color: "var(--blue)" }}>
            <span style={{ display: "inline-block", width: 8, height: 8, borderRadius: "50%",
                            background: "var(--blue)", marginRight: 8 }} />
            {statusLine}
          </div>
        )}
        {launchError && (
          <div style={{ marginTop: 12, fontSize: 12.5, color: "var(--red)" }}>{launchError}</div>
        )}
        {runDone && runErrors.length > 0 && (
          <div style={{ marginTop: 12, padding: "10px 12px", borderRadius: 10,
                         border: "1px solid var(--red)", color: "var(--red)", fontSize: 12.5 }}>
            <b>Run finished with errors:</b>
            {runErrors.map((e, i) => <div key={i} style={{ marginTop: 4 }}>{e}</div>)}
          </div>
        )}
        {runDone && runErrors.length === 0 && (
          <div style={{ marginTop: 12, fontSize: 12.5, color: "var(--green)" }}>
            ✓ Run finished — results below, reports on the Reports page.
          </div>
        )}
      </div>

      <div style={{ display: "flex", gap: 8, marginBottom: 16 }}>
        <button className={`btn ${tab === "core_basis" ? "" : "secondary"}`}
                onClick={() => setTab("core_basis")}>Core BASIS history</button>
        <button className={`btn ${tab === "flows" ? "" : "secondary"}`}
                onClick={() => setTab("flows")}>Functional flows history</button>
      </div>

      {tab === "flows" && (
        <div className="list">
          {flowRuns.length === 0 && (
            <div className="row"><span className="rsub">No flow runs yet — start one above with &quot;Functional flows&quot; checked.</span></div>
          )}
          {flowRuns.map((r, i) => {
            const worst = worstSeverity(r.checks);
            const failed = r.checks.filter(c => !c.ok).length;
            return (
              <div className="row" key={`${r.run_id}-${r.sid}-${r.flow_id}-${i}`}>
                <span className="rdot" style={{
                  background: r.error ? "var(--red)" : worst ? "var(--orange)" : "var(--green)"
                }} />
                <div className="grow">
                  <div className="rtitle">{r.sid} · {r.flow_id}</div>
                  <div className="rsub">
                    {r.mode} mode{r.started_at ? ` · ${r.started_at}` : ""}
                    {r.error ? ` · error: ${r.error}` : ""}
                  </div>
                </div>
                {r.error ? <span className="sev critical">ERROR</span>
                  : worst ? <span className={`sev ${worst}`}>{failed} {worst.toUpperCase()}</span>
                  : <span className="sev clean">CLEAN</span>}
              </div>
            );
          })}
        </div>
      )}

      {tab === "core_basis" && (
        <div className="list">
          {coreBasisRuns.length === 0 && (
            <div className="row"><span className="rsub">No Core BASIS runs yet — select a system above and Execute.</span></div>
          )}
          {coreBasisRuns.map((r, i) => (
            <div className="row" key={`${r.run_id}-${r.sid}-${i}`}>
              <span className="rdot" style={{ background: r.ok ? "var(--green)" : "var(--orange)" }} />
              <div className="grow">
                <div className="rtitle">{r.sid} · Core BASIS sweep</div>
                <div className="rsub">
                  {r.started_at}
                  {r.notes.length > 0 ? ` · ${r.notes[0]}` : ""}
                </div>
              </div>
              {r.ok
                ? <span className="sev clean">CLEAN</span>
                : <span className="sev medium">{r.anomaly_count} ANOMALIES</span>}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
