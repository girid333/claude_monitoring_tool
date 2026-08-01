"use client";

/* Landscape view — implements design/mockup.html against the real API.
   Island subscribes to /api/events/{run_id} (SSE) per D-016.
   Visual details (spacing, badges, sections) follow the mockup exactly —
   extend there first, then port here. */

import { useEffect, useMemo, useState } from "react";
import { apiBase } from "./lib/api";

const API = apiBase();

type SystemRow = {
  sid: string; release_tag: string; auth: string; tags: string[];
  last_run: { ok: boolean; flow_id: string; checks: { ok: boolean; severity: string }[] } | null;
};
type IslandEvent = { type: string; sid?: string; flow_id?: string; ok?: boolean };

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
  const [runId, setRunId] = useState<string | null>(null);
  const [islandEvents, setIslandEvents] = useState<IslandEvent[]>([]);
  const [includeCoreBasis, setIncludeCoreBasis] = useState(true);
  const [includeFlows, setIncludeFlows] = useState(true);

  useEffect(() => {
    fetch(`${API}/api/systems`).then(r => r.json()).then(setSystems).catch(() => {});
  }, [runId]);

  useEffect(() => {
    if (!runId) return;
    const es = new EventSource(`${API}/api/events/${runId}`);
    es.onmessage = e => {
      const ev: IslandEvent = JSON.parse(e.data);
      setIslandEvents(prev => [...prev, ev]);
      if (ev.type === "run_done") { es.close(); setRunId(null); }
    };
    return () => es.close();
  }, [runId]);

  const startRun = async () => {
    const res = await fetch(`${API}/api/run`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        sids: [...selected],
        flows: includeFlows ? ["flows/sm37_cancelled_jobs.json"] : [],
        include_core_basis: includeCoreBasis,   // D-026: was hardcoded false
      }),
    });
    setIslandEvents([]);
    setRunId((await res.json()).run_id);
  };

  const islandChips = useMemo(
    () => islandEvents.filter(e =>
      ["flow_done", "flow_start", "core_basis_start", "core_basis_done"].includes(e.type)),
    [islandEvents]);

  return (
    <div style={{ maxWidth: 1100 }}>
      {/* Dynamic Island */}
      {runId && (
        <div style={{ display: "flex", justifyContent: "center", marginBottom: 30 }}>
          <div className="panel" style={{
            borderRadius: 28, padding: "12px 20px", display: "flex",
            alignItems: "center", gap: 16, maxWidth: 640, width: "100%",
            background: "rgba(12,12,14,.9)",
          }}>
            <span style={{
              width: 9, height: 9, borderRadius: "50%", background: "var(--green)",
            }} />
            <div>
              <div style={{ fontSize: 13, fontWeight: 600 }}>Bulk run</div>
              <div style={{ fontSize: 11, color: "var(--text-3)" }}>Run {runId}</div>
            </div>
            <div style={{ display: "flex", gap: 6, flex: 1, justifyContent: "flex-end", flexWrap: "wrap" }}>
              {islandChips.map((e, i) => {
                const isStart = e.type === "flow_start" || e.type === "core_basis_start";
                const label = e.type.startsWith("core_basis")
                  ? `${e.sid} BASIS` : e.flow_id?.split("_")[0]?.toUpperCase();
                return (
                  <span key={i} style={{
                    fontSize: 10.5, fontWeight: 600, padding: "3.5px 9px",
                    borderRadius: 999, border: "1px solid var(--hairline)",
                    color: isStart ? "var(--blue)" : e.ok ? "var(--green)" : "var(--orange)",
                  }}>
                    {label}{isStart ? " …" : (e.ok ? " ✓" : " !")}
                  </span>
                );
              })}
            </div>
          </div>
        </div>
      )}

      <h1 style={{ fontSize: 32, fontWeight: 700, letterSpacing: "-0.022em" }}>Landscape</h1>
      <p style={{ fontSize: 13.5, color: "var(--text-2)", margin: "4px 0 26px" }}>
        <b style={{ color: "var(--text)" }}>{systems.length} systems</b>
        {selected.size > 0 && <> · {selected.size} selected
          <label style={{ marginLeft: 14, fontSize: 12, color: "var(--text-2)", cursor: "pointer" }}>
            <input type="checkbox" checked={includeCoreBasis}
                   onChange={e => setIncludeCoreBasis(e.target.checked)}
                   style={{ marginRight: 5, verticalAlign: -1 }} />
            Core BASIS
          </label>
          <label style={{ marginLeft: 10, fontSize: 12, color: "var(--text-2)", cursor: "pointer" }}>
            <input type="checkbox" checked={includeFlows}
                   onChange={e => setIncludeFlows(e.target.checked)}
                   style={{ marginRight: 5, verticalAlign: -1 }} />
            Functional flows
          </label>
          <button onClick={startRun} disabled={!!runId || (!includeCoreBasis && !includeFlows)} style={{
            marginLeft: 12, background: "var(--blue)", color: "#fff", border: 0,
            borderRadius: 999, padding: "5px 14px", fontSize: 12.5,
            fontWeight: 600, cursor: "pointer",
            opacity: (runId || (!includeCoreBasis && !includeFlows)) ? 0.5 : 1,
          }}>Run sweep</button></>}
      </p>

      <div style={{
        display: "grid", gap: 16,
        gridTemplateColumns: "repeat(auto-fill,minmax(230px,1fr))",
      }}>
        {systems.map(s => {
          const h = healthOf(s);
          const on = selected.has(s.sid);
          return (
            <div key={s.sid} className="panel card" tabIndex={0}
              onClick={() => setSelected(prev => {
                const n = new Set(prev); n.has(s.sid) ? n.delete(s.sid) : n.add(s.sid); return n;
              })}
              style={{ padding: "18px 18px 16px", outline: on ? "2px solid var(--blue)" : "none" }}>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <div>
                  <div style={{ fontSize: 22, fontWeight: 700 }}>{s.sid}</div>
                  <div style={{ fontSize: 11.5, color: "var(--text-3)", marginTop: 2 }}>
                    {s.release_tag} · {s.auth.toUpperCase()}
                  </div>
                </div>
                <span style={{
                  width: 11, height: 11, borderRadius: "50%", marginTop: 5,
                  background: DOT[h], boxShadow: `0 0 10px ${DOT[h]}`,
                }} />
              </div>
              <div style={{
                marginTop: 14, paddingTop: 12, borderTop: "1px solid var(--hairline)",
                fontSize: 12, color: "var(--text-2)",
              }}>
                {s.last_run
                  ? `${s.last_run.flow_id} · ${s.last_run.ok ? "clean" : "anomalies"}`
                  : "no runs yet"}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
