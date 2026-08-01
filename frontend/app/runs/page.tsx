"use client";

/* Runs — iOS inset-grouped list (D-016) over both run kinds the backend
   tracks separately (D-019's store split: flow_runs vs core_basis_runs,
   different schemas, same UI). */

import { useEffect, useState } from "react";

const API = process.env.NEXT_PUBLIC_API ?? "http://localhost:8000";

type FlowRun = {
  run_id: string; sid: string; flow_id: string; mode: string; ok: boolean;
  error: string | null; started_at?: string;
  checks: { ok: boolean; severity: string; message: string }[];
};
type CoreBasisRun = {
  run_id: string; sid: string; ok: boolean; anomaly_count: number;
  notes: string[]; started_at: string;
};

function worstSeverity(checks: FlowRun["checks"]): string | null {
  const order = ["critical", "high", "medium", "low", "info"];
  const failed = checks.filter(c => !c.ok).map(c => c.severity);
  return order.find(s => failed.includes(s)) ?? null;
}

export default function Runs() {
  const [flowRuns, setFlowRuns] = useState<FlowRun[]>([]);
  const [coreBasisRuns, setCoreBasisRuns] = useState<CoreBasisRun[]>([]);
  const [tab, setTab] = useState<"flows" | "core_basis">("flows");

  useEffect(() => {
    fetch(`${API}/api/runs?limit=50`).then(r => r.json()).then(setFlowRuns).catch(() => {});
    fetch(`${API}/api/runs/core-basis?limit=50`).then(r => r.json()).then(setCoreBasisRuns).catch(() => {});
  }, []);

  return (
    <div style={{ maxWidth: 900 }}>
      <h1 className="ltitle">Runs</h1>
      <p className="lsub">
        <b>{flowRuns.length} flow runs</b> · <b>{coreBasisRuns.length} Core BASIS runs</b>
      </p>

      <div style={{ display: "flex", gap: 8, marginBottom: 16 }}>
        <button className={`btn ${tab === "flows" ? "" : "secondary"}`}
                onClick={() => setTab("flows")}>Functional Sanity (flows)</button>
        <button className={`btn ${tab === "core_basis" ? "" : "secondary"}`}
                onClick={() => setTab("core_basis")}>Core BASIS</button>
      </div>

      {tab === "flows" && (
        <div className="list">
          {flowRuns.length === 0 && (
            <div className="row"><span className="rsub">No flow runs yet — trigger one from Landscape.</span></div>
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
            <div className="row"><span className="rsub">No Core BASIS runs yet — start a run with &quot;include_core_basis&quot;.</span></div>
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
