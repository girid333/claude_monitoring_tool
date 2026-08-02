"use client";

/* Runs — HISTORY ONLY (D-031).

   Execution lives in exactly one place: the Landscape page. This page used
   to have its own launcher (D-029), which meant two competing run buttons
   and two copies of the live-status logic. Landscape won because that's
   where the systems are, and the run state now lives in RunContext so
   progress follows you across tabs anyway. */

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { apiBase } from "../lib/api";
import { useRun } from "../components/RunContext";

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
type TcodeAnomalies = { tcode: string; name: string; anomalies: string[]; summary: string };
type RunDetail = {
  run_id: string; sid: string; anomaly_count: number; notes: string[];
  tcodes_with_anomalies: TcodeAnomalies[];
  tcodes_clean: string[];
  external_checks: { type?: string; url?: string; status?: string; details?: string }[];
};

function worstSeverity(checks: FlowRun["checks"]): string | null {
  const order = ["critical", "high", "medium", "low", "info"];
  const failed = checks.filter(c => !c.ok).map(c => c.severity);
  return order.find(s => failed.includes(s)) ?? null;
}

export default function Runs() {
  const [flowRuns, setFlowRuns] = useState<FlowRun[]>([]);
  const [coreBasisRuns, setCoreBasisRuns] = useState<CoreBasisRun[]>([]);
  const [tab, setTab] = useState<"flows" | "core_basis">("core_basis");
  const [loadError, setLoadError] = useState("");
  // D-032: click a row to expand its per-T-code anomaly summary
  const [openKey, setOpenKey] = useState<string | null>(null);
  const [detail, setDetail] = useState<Record<string, RunDetail | "loading" | "error">>({});
  const run = useRun();

  const toggleDetail = useCallback((r: CoreBasisRun) => {
    const key = `${r.run_id}-${r.sid}`;
    if (openKey === key) { setOpenKey(null); return; }
    setOpenKey(key);
    if (detail[key] && detail[key] !== "error") return;
    setDetail(d => ({ ...d, [key]: "loading" }));
    fetch(`${API}/api/runs/core-basis/${r.run_id}/${r.sid}`)
      .then(res => { if (!res.ok) throw new Error(String(res.status)); return res.json(); })
      .then(j => setDetail(d => ({ ...d, [key]: j })))
      .catch(() => setDetail(d => ({ ...d, [key]: "error" })));
  }, [openKey, detail]);

  const loadHistory = useCallback(() => {
    fetch(`${API}/api/runs?limit=50`).then(r => r.json()).then(setFlowRuns)
      .catch(err => setLoadError(`Could not reach the backend at ${API}: ${err instanceof Error ? err.message : String(err)}`));
    fetch(`${API}/api/runs/core-basis?limit=50`).then(r => r.json()).then(setCoreBasisRuns).catch(() => {});
  }, []);

  // Refresh when a run finishes (run.done flips) so results appear without
  // a manual reload, wherever the run was started from.
  useEffect(() => { loadHistory(); }, [loadHistory, run.done]);

  return (
    <div style={{ maxWidth: 900 }} className="page-enter">
      <h1 className="ltitle">Runs</h1>
      <p className="lsub">
        <b>{coreBasisRuns.length} Core BASIS runs</b> · <b>{flowRuns.length} flow runs</b>
        {" · start a run from "}
        <Link href="/" style={{ color: "var(--blue)" }}>Landscape</Link>
      </p>

      {run.isRunning && (
        <div className="panel" style={{ padding: "12px 16px", marginBottom: 18, borderRadius: 12,
                                         display: "flex", alignItems: "center", gap: 10 }}>
          <span className="pulse" style={{ width: 8, height: 8, borderRadius: "50%", background: "var(--blue)" }} />
          <span style={{ fontSize: 12.5, color: "var(--text-2)" }}>
            {run.phase || "Run in progress…"}
          </span>
          <Link href="/" className="btn secondary" style={{ marginLeft: "auto", fontSize: 11.5,
                                                             padding: "4px 10px", textDecoration: "none" }}>
            View progress
          </Link>
        </div>
      )}

      {loadError && (
        <div style={{ margin: "0 0 16px", padding: "10px 14px", borderRadius: 10,
                       border: "1px solid var(--red)", color: "var(--red)", fontSize: 12.5 }}>
          {loadError}
        </div>
      )}

      <div style={{ display: "flex", gap: 8, marginBottom: 16 }}>
        <button className={`btn ${tab === "core_basis" ? "" : "secondary"}`}
                onClick={() => setTab("core_basis")}>Core BASIS history</button>
        <button className={`btn ${tab === "flows" ? "" : "secondary"}`}
                onClick={() => setTab("flows")}>Functional flows history</button>
      </div>

      {tab === "flows" && (
        <div className="list">
          {flowRuns.length === 0 && (
            <div className="row"><span className="rsub">
              No flow runs yet — run one from <Link href="/" style={{ color: "var(--blue)" }}>Landscape</Link> with
              &quot;Functional flows&quot; ticked.
            </span></div>
          )}
          {flowRuns.map((r, i) => {
            const worst = worstSeverity(r.checks);
            const failed = r.checks.filter(c => !c.ok).length;
            return (
              <div className="row rise" key={`${r.run_id}-${r.sid}-${r.flow_id}-${i}`}
                   style={{ ["--i" as string]: Math.min(i, 10) }}>
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
            <div className="row"><span className="rsub">
              No Core BASIS runs yet — go to <Link href="/" style={{ color: "var(--blue)" }}>Landscape</Link>,
              select a system, tick &quot;Core BASIS sweep&quot; and press Execute.
            </span></div>
          )}
          {coreBasisRuns.map((r, i) => {
            const key = `${r.run_id}-${r.sid}`;
            const open = openKey === key;
            const d = detail[key];
            return (
              <div key={`${key}-${i}`}>
                <div className="row rise" style={{ ["--i" as string]: Math.min(i, 10), cursor: "pointer" }}
                     onClick={() => toggleDetail(r)}>
                  <span className="rdot" style={{ background: r.ok ? "var(--green)" : "var(--orange)" }} />
                  <div className="grow">
                    <div className="rtitle">
                      <span style={{ display: "inline-block", width: 12, color: "var(--text-3)",
                                      transform: open ? "rotate(90deg)" : "none",
                                      transition: "transform .2s var(--ease-out)" }}>›</span>
                      {r.sid} · Core BASIS sweep
                    </div>
                    <div className="rsub" style={{ paddingLeft: 12 }}>
                      {r.started_at}
                      {r.notes.length > 0 ? ` · ${r.notes[0]}` : ""}
                    </div>
                  </div>
                  {r.ok
                    ? <span className="sev clean">CLEAN</span>
                    : <span className="sev medium">{r.anomaly_count} ANOMALIES</span>}
                </div>

                {open && (
                  <div style={{ padding: "12px 18px 16px 44px", borderBottom: "1px solid var(--hairline)",
                                 background: "rgba(255,255,255,.02)" }} className="page-enter">
                    {d === "loading" && <span className="rsub">Loading summary…</span>}
                    {d === "error" && <span style={{ color: "var(--red)", fontSize: 12.5 }}>
                      Could not load this run&apos;s detail.</span>}
                    {d && d !== "loading" && d !== "error" && (
                      <>
                        {d.tcodes_with_anomalies.length === 0 && (
                          <div style={{ fontSize: 12.5, color: "var(--green)" }}>
                            No anomalies — all {d.tcodes_clean.length} T-codes clean.
                          </div>
                        )}
                        {d.tcodes_with_anomalies.map(t => (
                          <div key={t.tcode} style={{ marginBottom: 12 }}>
                            <div style={{ fontSize: 12.5, fontWeight: 600, marginBottom: 4 }}>
                              {t.tcode} <span style={{ color: "var(--text-3)", fontWeight: 400 }}>· {t.name}</span>
                              <span className="sev medium" style={{ marginLeft: 8 }}>{t.anomalies.length}</span>
                            </div>
                            <ul style={{ margin: 0, paddingLeft: 18 }}>
                              {t.anomalies.slice(0, 8).map((a, j) => (
                                <li key={j} style={{ fontSize: 12, color: "var(--text-2)", marginBottom: 2 }}>{a}</li>
                              ))}
                              {t.anomalies.length > 8 && (
                                <li style={{ fontSize: 11.5, color: "var(--text-3)" }}>
                                  …and {t.anomalies.length - 8} more — see the full report
                                </li>
                              )}
                            </ul>
                          </div>
                        ))}
                        {d.external_checks.length > 0 && (
                          <div style={{ marginTop: 10, paddingTop: 10, borderTop: "1px solid var(--hairline)" }}>
                            <div style={{ fontSize: 12.5, fontWeight: 600, marginBottom: 4 }}>
                              Infrastructure &amp; URL checks
                            </div>
                            {d.external_checks.slice(0, 8).map((c, j) => (
                              <div key={j} style={{ fontSize: 12, color: "var(--text-2)" }}>
                                {c.type}{c.url ? ` (${c.url})` : ""} — <b>{c.status}</b>
                                {c.details ? `: ${String(c.details).slice(0, 120)}` : ""}
                              </div>
                            ))}
                          </div>
                        )}
                        {d.tcodes_clean.length > 0 && (
                          <div style={{ marginTop: 10, fontSize: 11.5, color: "var(--text-3)" }}>
                            Clean: {d.tcodes_clean.join(", ")}
                          </div>
                        )}
                      </>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
