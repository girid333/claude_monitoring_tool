"use client";

/* D-044 SANDBOX — SAP GUI bridge-agent record/replay.

   This page is DELIBERATELY separate from Recorder/Landscape/Reports. Per
   Giri's instruction it is an isolated, additive section: a "SAP GUI
   (sandbox)" flow lives in its own backend directory (flows_sapgui/, not
   flows/) and is driven entirely through /api/sapgui/* — none of which
   touches the existing WebGUI recorder or Core BASIS pipeline. Nothing
   here appears on Landscape's flow picker, and nothing from Landscape
   appears here.

   How it works: a small Windows program (agent/sapgui_bridge_agent.py)
   runs on a workstation that has SAP GUI for Windows installed and a
   logged-in session. It connects OUTBOUND to this backend over
   WebSocket — the container never has to reach the workstation. Pair it
   below, then record/replay using SAP GUI Scripting on that machine. */

import { useEffect, useRef, useState } from "react";
import { apiBase } from "../../lib/api";

const API = apiBase();
const WS_BASE = API.replace(/^http/, "ws");

type AgentRow = {
  agent_id: string; hostname: string; username: string;
  sap_gui_version: string; scripting_enabled: boolean | null;
  connected_seconds: number; last_seen_seconds: number;
};
type FlowRow = {
  flow_id: string; description: string; step_count: number;
  applies_to: string[]; recorded_by_agent?: string;
};
type StepRow = { index: number; act: string; sid: string; label: string; value: unknown; description: string };
type LiveEvent = Record<string, unknown>;

export default function SapGuiSandboxPage() {
  const [agents, setAgents] = useState<AgentRow[]>([]);
  const [pairingCode, setPairingCode] = useState<string | null>(null);
  const [pairingBusy, setPairingBusy] = useState(false);
  const [error, setError] = useState("");

  const [recordAgentId, setRecordAgentId] = useState("");
  const [tcode, setTcode] = useState("");
  const [sid, setSid] = useState("");
  const [flowId, setFlowId] = useState("");
  const [description, setDescription] = useState("");
  const [recording, setRecording] = useState<{ agentId: string; jobId: string } | null>(null);
  const [recordingStatus, setRecordingStatus] = useState("");
  const [liveEvents, setLiveEvents] = useState<LiveEvent[]>([]);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState<string | null>(null);
  const recordEsRef = useRef<EventSource | null>(null);

  const [flows, setFlows] = useState<FlowRow[]>([]);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [flowSteps, setFlowSteps] = useState<StepRow[]>([]);
  const [overrides, setOverrides] = useState<Record<string, Record<number, string>>>({});
  const [replayAgentId, setReplayAgentId] = useState("");
  const [replayEvents, setReplayEvents] = useState<LiveEvent[]>([]);
  const [replaying, setReplaying] = useState<string | null>(null);
  const replayEsRef = useRef<EventSource | null>(null);

  const loadAgents = () => {
    fetch(`${API}/api/sapgui/agents`).then(r => r.json()).then(setAgents).catch(() => {});
  };
  const loadFlows = () => {
    fetch(`${API}/api/sapgui/flows`).then(r => r.json()).then(setFlows).catch(() => {});
  };

  useEffect(() => {
    loadAgents();
    loadFlows();
    const t = setInterval(loadAgents, 3000);
    return () => clearInterval(t);
  }, []);

  const pair = async () => {
    setPairingBusy(true); setError("");
    try {
      const r = await fetch(`${API}/api/sapgui/pair/start`, { method: "POST" });
      const j = await r.json();
      setPairingCode(j.code);
    } catch (err) {
      setError(`Could not request a pairing code: ${err}`);
    } finally { setPairingBusy(false); }
  };

  const agentCommand = pairingCode
    ? `python sapgui_bridge_agent.py --server ${WS_BASE} --code ${pairingCode}`
    : "";

  const startRecording = async () => {
    if (!recordAgentId) return;
    setBusy(true); setError(""); setSaved(null); setLiveEvents([]); setRecordingStatus("");
    try {
      const r = await fetch(`${API}/api/sapgui/record/start`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ agent_id: recordAgentId, tcode: tcode.trim().toUpperCase(), sid }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`);
      const j = await r.json();
      setRecording({ agentId: recordAgentId, jobId: j.job_id });
      setFlowId(prev => prev || (tcode ? `${tcode.trim().toLowerCase()}_sapgui` : "sapgui_flow"));

      const es = new EventSource(`${API}/api/sapgui/events/${recordAgentId}/${j.job_id}`);
      es.onmessage = (ev) => {
        const data = JSON.parse(ev.data);
        setLiveEvents(prev => [...prev, data]);
        // The agent reports connect/navigation failures as kind:"error" right
        // after Start Recording — surface it prominently instead of leaving
        // the operator staring at a silent "0 steps" with no explanation.
        if (data.kind === "error") {
          setError(`Agent could not start recording: ${data.error || data.message || "unknown error"}`);
          es.close();
          setRecording(null);
        } else if (data.kind === "status") {
          setRecordingStatus(String(data.message || ""));
        }
      };
      recordEsRef.current = es;
    } catch (err) {
      setError(`Could not start recording: ${err instanceof Error ? err.message : String(err)}`);
    } finally { setBusy(false); }
  };

  const stopRecording = async () => {
    if (!recording) return;
    setBusy(true); setError("");
    try {
      const r = await fetch(`${API}/api/sapgui/record/stop`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          agent_id: recording.agentId, job_id: recording.jobId,
          flow_id: flowId || "sapgui_flow", description,
        }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`);
      const j = await r.json();
      setSaved(`Saved ${j.flow_id} (${j.step_count} steps) → ${j.path}`);
      recordEsRef.current?.close();
      setRecording(null);
      loadFlows();
    } catch (err) {
      setError(`Could not stop recording: ${err instanceof Error ? err.message : String(err)}`);
    } finally { setBusy(false); }
  };

  const viewSteps = async (id: string) => {
    if (expanded === id) { setExpanded(null); return; }
    try {
      const r = await fetch(`${API}/api/sapgui/flows/${id}`);
      const j = await r.json();
      setFlowSteps(j.steps ?? []);
      setExpanded(id);
    } catch (err) { setError(`Could not load flow: ${err}`); }
  };

  const deleteFlow = async (id: string) => {
    await fetch(`${API}/api/sapgui/flows/${id}`, { method: "DELETE" }).catch(() => {});
    if (expanded === id) setExpanded(null);
    loadFlows();
  };

  const setOverride = (flow_id: string, index: number, value: string) => {
    setOverrides(prev => ({ ...prev, [flow_id]: { ...(prev[flow_id] || {}), [index]: value } }));
  };

  const replay = async (flow_id: string) => {
    if (!replayAgentId) { setError("Pick an agent to replay with first."); return; }
    setError(""); setReplayEvents([]); setReplaying(flow_id);
    try {
      const ov = overrides[flow_id] || {};
      const r = await fetch(`${API}/api/sapgui/replay`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ agent_id: replayAgentId, flow_id, overrides: ov }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`);
      const j = await r.json();
      const es = new EventSource(`${API}/api/sapgui/events/${replayAgentId}/${j.job_id}`);
      es.onmessage = (ev) => {
        const data = JSON.parse(ev.data);
        setReplayEvents(prev => [...prev, data]);
        if (data.kind === "replay_done") es.close();
      };
      replayEsRef.current = es;
    } catch (err) {
      setError(`Could not start replay: ${err instanceof Error ? err.message : String(err)}`);
      setReplaying(null);
    }
  };

  return (
    <div style={{ maxWidth: 1200 }} className="page-enter">
      <h1 className="ltitle">SAP GUI (sandbox)</h1>
      <p className="lsub">
        An isolated, experimental record/replay path via SAP GUI Scripting on a Windows
        workstation — separate from the WebGUI Recorder above. It is not merged into
        Landscape or Reports yet; nothing here affects the existing engine. Complex screens
        (checkboxes, date ranges, multi-window dialogs) are handled natively here because
        SAP GUI Scripting exposes every control directly, the same foundation CBTA and
        Tricentis use.
      </p>

      {error && (
        <div style={{ margin: "0 0 16px", padding: "10px 14px", borderRadius: 10,
                       border: "1px solid var(--red)", color: "var(--red)", fontSize: 12.5 }}>
          {error}
        </div>
      )}
      {saved && (
        <div style={{ margin: "0 0 16px", padding: "10px 14px", borderRadius: 10,
                       border: "1px solid var(--green)", color: "var(--green)", fontSize: 12.5 }}>
          ✓ {saved}
        </div>
      )}

      {/* ── Pairing + agents ─────────────────────────────────────────── */}
      <div className="panel" style={{ padding: "14px 18px", marginBottom: 18, borderRadius: 14 }}>
        <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>Bridge agent</div>
        <div style={{ display: "flex", gap: 14, alignItems: "center", flexWrap: "wrap", marginBottom: 10 }}>
          <button className="btn" disabled={pairingBusy} onClick={pair}>
            {pairingBusy ? "Requesting…" : "Generate pairing code"}
          </button>
          {pairingCode && (
            <code style={{ fontSize: 12, padding: "4px 10px", borderRadius: 6,
                            background: "rgba(255,255,255,.06)" }}>
              {agentCommand}
            </code>
          )}
          <span style={{ fontSize: 11.5, color: "var(--text-3)" }}>
            Run that on the Windows workstation (5-minute code TTL). See agent/README for setup,
            or add <code>--fake</code> to try the sandbox without SAP GUI installed.
          </span>
        </div>
        {agents.length === 0 ? (
          <div className="rsub">No agents connected.</div>
        ) : (
          <table style={{ width: "100%", fontSize: 12.5, borderCollapse: "collapse" }}>
            <thead>
              <tr style={{ color: "var(--text-3)", textAlign: "left" }}>
                <th style={{ padding: "4px 8px" }}>Host</th>
                <th style={{ padding: "4px 8px" }}>User</th>
                <th style={{ padding: "4px 8px" }}>SAP GUI</th>
                <th style={{ padding: "4px 8px" }}>Scripting</th>
                <th style={{ padding: "4px 8px" }}>Last seen</th>
              </tr>
            </thead>
            <tbody>
              {agents.map(a => (
                <tr key={a.agent_id} style={{ borderTop: "1px solid var(--hairline)" }}>
                  <td style={{ padding: "4px 8px" }}>{a.hostname}</td>
                  <td style={{ padding: "4px 8px" }}>{a.username}</td>
                  <td style={{ padding: "4px 8px" }}>{a.sap_gui_version}</td>
                  <td style={{ padding: "4px 8px" }}>
                    {a.scripting_enabled ? "✓ enabled" : "✗ not confirmed"}
                  </td>
                  <td style={{ padding: "4px 8px" }}>{a.last_seen_seconds}s ago</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* ── Record ───────────────────────────────────────────────────── */}
      <div className="panel" style={{ padding: "14px 18px", marginBottom: 18, borderRadius: 14,
                                       display: "flex", gap: 14, alignItems: "center", flexWrap: "wrap" }}>
        {!recording ? (
          <>
            <label style={{ fontSize: 12.5, color: "var(--text-2)" }}>Agent</label>
            <select className="input" value={recordAgentId}
                    onChange={e => setRecordAgentId(e.target.value)} style={{ minWidth: 200 }}>
              <option value="">Select a connected agent…</option>
              {agents.map(a => (
                <option key={a.agent_id} value={a.agent_id}>{a.hostname} ({a.agent_id})</option>
              ))}
            </select>
            <label style={{ fontSize: 12.5, color: "var(--text-2)" }}>T-code</label>
            <input className="input" value={tcode} onChange={e => setTcode(e.target.value.toUpperCase())}
                   placeholder="SM37" style={{ width: 100 }} />
            <label style={{ fontSize: 12.5, color: "var(--text-2)" }}>SID (tag only)</label>
            <input className="input" value={sid} onChange={e => setSid(e.target.value.toUpperCase())}
                   placeholder="F4H" style={{ width: 80 }}
                   title="Just labels which system this flow applies to — the agent's already-logged-in session does the actual work" />
            <button className="btn" disabled={!recordAgentId || busy} onClick={startRecording}>
              {busy ? "Starting…" : "Start Recording"}
            </button>
          </>
        ) : (
          <>
            <span className="pulse" style={{ width: 9, height: 9, borderRadius: "50%",
                                              background: "var(--red)" }} />
            <span style={{ fontSize: 13, fontWeight: 600 }}>Recording on {recording.agentId}</span>
            <span style={{ fontSize: 12, color: "var(--text-3)" }}>
              {liveEvents.filter(e => e.kind === "step").length} steps
            </span>
            {recordingStatus && (
              <span style={{ fontSize: 11.5, color: "var(--green)" }}>{recordingStatus}</span>
            )}
            <label style={{ fontSize: 12.5, color: "var(--text-2)", marginLeft: 6 }}>Name</label>
            <input className="input" value={flowId} onChange={e => setFlowId(e.target.value)}
                   style={{ width: 180 }} />
            <input className="input" value={description}
                   onChange={e => setDescription(e.target.value)}
                   placeholder="description (optional)" style={{ width: 220 }} />
            <button className="btn" disabled={busy || !flowId.trim()} onClick={stopRecording}
                    style={{ marginLeft: "auto" }}>
              {busy ? "Saving…" : "Stop & Save Flow"}
            </button>
          </>
        )}
      </div>

      {recording && liveEvents.length > 0 && (
        <div className="panel" style={{ padding: "10px 16px", marginBottom: 18, borderRadius: 14,
                                         maxHeight: 220, overflowY: "auto", fontSize: 12 }}>
          {liveEvents.map((e, i) => (
            <div key={i} style={{ padding: "3px 0", color: "var(--text-2)" }}>
              {JSON.stringify(e)}
            </div>
          ))}
        </div>
      )}

      {/* ── Flow library ─────────────────────────────────────────────── */}
      <div className="panel" style={{ padding: "14px 18px", borderRadius: 14 }}>
        <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 10 }}>
          Sandbox flows ({flows.length})
        </div>
        <div style={{ display: "flex", gap: 10, alignItems: "center", marginBottom: 12 }}>
          <label style={{ fontSize: 12.5, color: "var(--text-2)" }}>Replay with agent</label>
          <select className="input" value={replayAgentId}
                  onChange={e => setReplayAgentId(e.target.value)} style={{ minWidth: 200 }}>
            <option value="">Select a connected agent…</option>
            {agents.map(a => (
              <option key={a.agent_id} value={a.agent_id}>{a.hostname} ({a.agent_id})</option>
            ))}
          </select>
        </div>
        {flows.length === 0 && <div className="rsub">No sandbox flows recorded yet.</div>}
        {flows.map(f => (
          <div key={f.flow_id} style={{ borderTop: "1px solid var(--hairline)", padding: "10px 0" }}>
            <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
              <span style={{ fontSize: 13, fontWeight: 600 }}>{f.flow_id}</span>
              <span style={{ fontSize: 12, color: "var(--text-3)" }}>{f.step_count} steps</span>
              {f.description && <span style={{ fontSize: 12, color: "var(--text-3)" }}>— {f.description}</span>}
              <div style={{ marginLeft: "auto", display: "flex", gap: 8 }}>
                <button className="btn secondary" style={{ fontSize: 11.5, padding: "3px 10px" }}
                        onClick={() => viewSteps(f.flow_id)}>
                  {expanded === f.flow_id ? "Hide steps" : "👁 View steps"}
                </button>
                <button className="btn secondary" style={{ fontSize: 11.5, padding: "3px 10px" }}
                        disabled={!replayAgentId} onClick={() => replay(f.flow_id)}>
                  ▶ Replay
                </button>
                <button className="btn secondary" style={{ fontSize: 11.5, padding: "3px 10px" }}
                        onClick={() => deleteFlow(f.flow_id)}>
                  Delete
                </button>
              </div>
            </div>

            {expanded === f.flow_id && (
              <table style={{ width: "100%", fontSize: 12, marginTop: 8, borderCollapse: "collapse" }}>
                <tbody>
                  {flowSteps.map(s => (
                    <tr key={s.index} style={{ borderTop: "1px solid var(--hairline)" }}>
                      <td style={{ padding: "4px 8px", color: "var(--text-3)", width: 30 }}>{s.index}</td>
                      <td style={{ padding: "4px 8px", color: "var(--blue)", width: 90 }}>{s.act}</td>
                      <td style={{ padding: "4px 8px" }}>{s.label}</td>
                      <td style={{ padding: "4px 8px" }}>
                        {s.value !== null && s.value !== undefined && (
                          <input className="input" style={{ fontSize: 11.5, padding: "2px 6px", width: 140 }}
                                 defaultValue={String(s.value)}
                                 onChange={e => setOverride(f.flow_id, s.index, e.target.value)} />
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}

            {replaying === f.flow_id && replayEvents.length > 0 && (
              <div style={{ marginTop: 8, padding: "8px 10px", borderRadius: 8,
                             background: "rgba(255,255,255,.04)", fontSize: 11.5 }}>
                {replayEvents.map((e, i) => <div key={i}>{JSON.stringify(e)}</div>)}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
