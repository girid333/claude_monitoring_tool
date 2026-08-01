"use client";

/* Recorder — start/stop UI for engine/recorder_service.py (D-004/D-020).
   Recording opens a HEADED browser window on whatever machine runs the
   backend (uvicorn main:app, per README's "run locally" model) — this page
   can only meaningfully be used when the backend has a display. That's a
   real constraint of interactive recording, not a bug in this UI. */

import { useEffect, useRef, useState } from "react";
import { apiBase } from "../lib/api";

const API = apiBase();

type SystemRow = { sid: string; release_tag: string; auth: string };
type RecorderStatus = {
  recording_id: string; sid: string; status: string; error: string | null;
  event_count: number; last_events: Record<string, unknown>[];
};

export default function Recorder() {
  const [systems, setSystems] = useState<SystemRow[]>([]);
  const [sid, setSid] = useState("");
  const [recordingId, setRecordingId] = useState<string | null>(null);
  const [status, setStatus] = useState<RecorderStatus | null>(null);
  const [flowId, setFlowId] = useState("");
  const [description, setDescription] = useState("");
  const [starting, setStarting] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [result, setResult] = useState<{ flow_id: string; step_count: number; path: string } | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    fetch(`${API}/api/systems`).then(r => r.json()).then(setSystems).catch(() => {});
  }, []);

  useEffect(() => {
    if (!recordingId) {
      if (pollRef.current) clearInterval(pollRef.current);
      return;
    }
    const poll = async () => {
      try {
        const res = await fetch(`${API}/api/recorder/status/${recordingId}`);
        if (!res.ok) return;
        setStatus(await res.json());
      } catch { /* transient — keep polling */ }
    };
    poll();
    pollRef.current = setInterval(poll, 1500);
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, [recordingId]);

  const startRecording = async () => {
    if (!sid) return;
    setStarting(true); setErrorMsg(null); setResult(null);
    try {
      const res = await fetch(`${API}/api/recorder/start`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sid }),
      });
      const body = await res.json();
      if (!res.ok) { setErrorMsg(body.detail ?? "failed to start recording"); return; }
      setRecordingId(body.recording_id);
    } catch (e) {
      setErrorMsg(String(e));
    } finally {
      setStarting(false);
    }
  };

  const stopRecording = async () => {
    if (!recordingId || !flowId) return;
    setStopping(true); setErrorMsg(null);
    try {
      const res = await fetch(`${API}/api/recorder/stop`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ recording_id: recordingId, flow_id: flowId, description }),
      });
      const body = await res.json();
      if (!res.ok) { setErrorMsg(body.detail ?? "failed to stop recording"); return; }
      setResult(body);
      setRecordingId(null); setStatus(null); setFlowId(""); setDescription("");
    } catch (e) {
      setErrorMsg(String(e));
    } finally {
      setStopping(false);
    }
  };

  const cancelRecording = async () => {
    if (!recordingId) return;
    await fetch(`${API}/api/recorder/cancel`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ recording_id: recordingId }),
    }).catch(() => {});
    setRecordingId(null); setStatus(null);
  };

  return (
    <div style={{ maxWidth: 720 }}>
      <h1 className="ltitle">Recorder</h1>
      <p className="lsub">
        Record once (D-004) on a scripting-enabled reference system — opens a
        real browser window you drive by hand; every click/change is captured
        as scripting ID + DOM metadata (D-002), so the flow replays on both
        scripting and DOM-only systems.
      </p>

      {!recordingId && (
        <div className="panel" style={{ padding: 20, marginBottom: 20 }}>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 10 }}>Start a recording</div>
          <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
            <select className="input" value={sid} onChange={e => setSid(e.target.value)}>
              <option value="">Select a system…</option>
              {systems.map(s => (
                <option key={s.sid} value={s.sid}>
                  {s.sid} · {s.release_tag} · {s.auth}
                </option>
              ))}
            </select>
            <button className="btn" disabled={!sid || starting} onClick={startRecording}>
              {starting ? "Starting…" : "Start Recording"}
            </button>
          </div>
          {systems.length === 0 && (
            <div style={{ fontSize: 11.5, color: "var(--text-3)", marginTop: 10 }}>
              No systems in the registry yet — add one to systems.json (see systems.example.json).
            </div>
          )}
        </div>
      )}

      {recordingId && (
        <div className="panel" style={{ padding: 20, marginBottom: 20 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 14 }}>
            <span style={{
              width: 9, height: 9, borderRadius: "50%", background: "var(--green)",
              boxShadow: "0 0 0 4px rgba(48,209,88,.15)",
            }} />
            <div style={{ fontSize: 13, fontWeight: 600 }}>Recording {status?.sid ?? sid}</div>
            <div style={{ fontSize: 11, color: "var(--text-3)" }}>{recordingId}</div>
          </div>

          <div style={{ fontSize: 12, color: "var(--text-2)", marginBottom: 10 }}>
            {status?.event_count ?? 0} events captured — drive the WebGUI window that just opened,
            then fill in a flow id below and stop when done.
          </div>

          {status?.last_events && status.last_events.length > 0 && (
            <div style={{
              fontSize: 11, color: "var(--text-3)", fontFamily: "monospace",
              background: "rgba(255,255,255,.03)", borderRadius: 8, padding: 10,
              marginBottom: 14, maxHeight: 120, overflow: "auto",
            }}>
              {status.last_events.map((e, i) => (
                <div key={i}>{JSON.stringify(e)}</div>
              ))}
            </div>
          )}

          <div style={{ display: "flex", gap: 10, marginBottom: 10 }}>
            <input className="input" style={{ flex: 1 }} placeholder="flow_id (e.g. sm37_cancelled_jobs)"
                   value={flowId} onChange={e => setFlowId(e.target.value)} />
          </div>
          <textarea className="input" style={{ width: "100%", marginBottom: 12, minHeight: 60 }}
                    placeholder="description (optional)"
                    value={description} onChange={e => setDescription(e.target.value)} />
          <div style={{ display: "flex", gap: 10 }}>
            <button className="btn" disabled={!flowId || stopping} onClick={stopRecording}>
              {stopping ? "Stopping…" : "Stop & Save Flow"}
            </button>
            <button className="btn danger" onClick={cancelRecording}>Cancel</button>
          </div>
        </div>
      )}

      {errorMsg && (
        <div className="panel" style={{ padding: 16, marginBottom: 20, borderColor: "rgba(255,69,58,.4)" }}>
          <span style={{ color: "var(--red)", fontSize: 13 }}>{errorMsg}</span>
        </div>
      )}

      {result && (
        <div className="panel" style={{ padding: 16 }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: "var(--green)", marginBottom: 4 }}>
            Saved {result.flow_id}
          </div>
          <div style={{ fontSize: 12, color: "var(--text-2)" }}>
            {result.step_count} steps → {result.path}
          </div>
        </div>
      )}
    </div>
  );
}
