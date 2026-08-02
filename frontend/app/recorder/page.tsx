"use client";

/* Recorder — drive a server-side SAP session and capture it as a flow (D-033).

   The browser runs HEADLESS in the backend container; what you see here is a
   live JPEG screencast of it, and your clicks/keystrokes are forwarded back
   into it over CDP. That is what makes recording possible in a container —
   the previous design needed a real display and was disabled with a 501. */

import { useCallback, useEffect, useRef, useState } from "react";
import { apiBase } from "../lib/api";

const API = apiBase();

type SystemRow = { sid: string; system_name?: string; release_tag: string; auth: string };
type Step = { act: string; target: string; value: unknown; resolved_by: string };

export default function RecorderPage() {
  const [systems, setSystems] = useState<SystemRow[]>([]);
  const [sid, setSid] = useState("");
  const [recId, setRecId] = useState<string | null>(null);
  const [frame, setFrame] = useState<string | null>(null);
  const [steps, setSteps] = useState<Step[]>([]);
  const [viewport, setViewport] = useState({ width: 1280, height: 800 });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [flowId, setFlowId] = useState("");
  const [saved, setSaved] = useState<string | null>(null);
  const imgRef = useRef<HTMLImageElement>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    fetch(`${API}/api/systems`).then(r => r.json()).then((s: SystemRow[]) => {
      setSystems(s);
      if (s.length) setSid(prev => prev || s[0].sid);
    }).catch(err => setError(`Could not reach the backend at ${API}: ${err}`));
  }, []);

  // Poll frames + step list while recording.
  useEffect(() => {
    if (!recId) return;
    const tick = async () => {
      try {
        const [f, s] = await Promise.all([
          fetch(`${API}/api/recorder/frame/${recId}`).then(r => r.json()),
          fetch(`${API}/api/recorder/status/${recId}`).then(r => r.json()),
        ]);
        if (f.frame) setFrame(f.frame);
        if (s.steps) setSteps(s.steps);
        if (s.viewport) setViewport(s.viewport);
      } catch { /* transient; the next tick retries */ }
    };
    void tick();
    pollRef.current = setInterval(tick, 700);
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, [recId]);

  const send = useCallback(async (events: Record<string, unknown>[]) => {
    if (!recId) return;
    try {
      await fetch(`${API}/api/recorder/input`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ recording_id: recId, events }),
      });
    } catch (err) { setError(`Input failed: ${err}`); }
  }, [recId]);

  const start = async () => {
    setBusy(true); setError(""); setSaved(null); setSteps([]);
    try {
      const res = await fetch(`${API}/api/recorder/start`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sid }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
      const j = await res.json();
      setRecId(j.recording_id);
      if (j.viewport) setViewport(j.viewport);
      setFlowId(prev => prev || `${sid.toLowerCase()}_flow`);
    } catch (err) {
      setError(`Could not start recording: ${err instanceof Error ? err.message : String(err)}`);
    } finally { setBusy(false); }
  };

  const stop = async () => {
    if (!recId) return;
    setBusy(true); setError("");
    try {
      const res = await fetch(`${API}/api/recorder/stop`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ recording_id: recId, flow_id: flowId || `${sid.toLowerCase()}_flow` }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
      const j = await res.json();
      setSaved(`Saved ${j.flow_id} (${j.step_count} steps) → ${j.path}`);
      setRecId(null); setFrame(null);
    } catch (err) {
      setError(`Could not stop recording: ${err instanceof Error ? err.message : String(err)}`);
    } finally { setBusy(false); }
  };

  const cancel = async () => {
    if (!recId) return;
    setBusy(true);
    try {
      await fetch(`${API}/api/recorder/cancel`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ recording_id: recId }),
      });
    } catch { /* closing anyway */ }
    setRecId(null); setFrame(null); setSteps([]); setBusy(false);
  };

  // Map a click on the rendered image back to browser viewport pixels.
  const onImgClick = (e: React.MouseEvent<HTMLImageElement>) => {
    const img = imgRef.current;
    if (!img) return;
    const r = img.getBoundingClientRect();
    const x = ((e.clientX - r.left) / r.width) * viewport.width;
    const y = ((e.clientY - r.top) / r.height) * viewport.height;
    void send([{ kind: "click", x: Math.round(x), y: Math.round(y) }]);
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (!recId) return;
    e.preventDefault();
    if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
      void send([{ kind: "text", text: e.key }]);
    } else {
      void send([{ kind: "key", key: e.key, code: e.code }]);
    }
  };

  const onWheel = (e: React.WheelEvent<HTMLImageElement>) => {
    const img = imgRef.current;
    if (!img) return;
    const r = img.getBoundingClientRect();
    void send([{
      kind: "wheel",
      x: Math.round(((e.clientX - r.left) / r.width) * viewport.width),
      y: Math.round(((e.clientY - r.top) / r.height) * viewport.height),
      dx: e.deltaX, dy: e.deltaY,
    }]);
  };

  return (
    <div style={{ maxWidth: 1400 }} className="page-enter">
      <h1 className="ltitle">Recorder</h1>
      <p className="lsub">
        Drive a real SAP session below and every interaction is captured as a replayable
        step. The browser runs on the server — this is a live view of it, so recording
        works in a container with no display.
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
          ✓ {saved} — run it from Landscape with &quot;Functional flows&quot; ticked.
        </div>
      )}

      <div className="panel" style={{ padding: "14px 18px", marginBottom: 18, borderRadius: 14,
                                       display: "flex", gap: 14, alignItems: "center", flexWrap: "wrap" }}>
        {!recId ? (
          <>
            <label style={{ fontSize: 12.5, color: "var(--text-2)" }}>System</label>
            <select className="input" value={sid} onChange={e => setSid(e.target.value)}
                    style={{ minWidth: 220 }}>
              {systems.length === 0 && <option value="">No systems — add one first</option>}
              {systems.map(s => (
                <option key={s.sid} value={s.sid}>
                  {s.system_name ? `${s.system_name} (${s.sid})` : s.sid} · {s.auth}
                </option>
              ))}
            </select>
            <button className="btn" disabled={!sid || busy} onClick={start}>
              {busy ? "Starting…" : "Start Recording"}
            </button>
          </>
        ) : (
          <>
            <span className="pulse" style={{ width: 9, height: 9, borderRadius: "50%",
                                              background: "var(--red)" }} />
            <span style={{ fontSize: 13, fontWeight: 600 }}>Recording {sid}</span>
            <span style={{ fontSize: 12, color: "var(--text-3)" }}>{steps.length} steps captured</span>
            <label style={{ fontSize: 12.5, color: "var(--text-2)", marginLeft: 10 }}>Flow name</label>
            <input className="input" value={flowId} onChange={e => setFlowId(e.target.value)}
                   placeholder="sm37_cancelled_jobs" style={{ width: 220 }} />
            <button className="btn" disabled={busy} onClick={stop} style={{ marginLeft: "auto" }}>
              {busy ? "Saving…" : "Stop & Save Flow"}
            </button>
            <button className="btn secondary" disabled={busy} onClick={cancel}>Discard</button>
          </>
        )}
      </div>

      {recId && (
        <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) 300px", gap: 18 }}>
          <div className="panel" style={{ padding: 10, borderRadius: 14, overflow: "hidden" }}
               tabIndex={0} onKeyDown={onKeyDown}>
            {frame ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img ref={imgRef} alt="Live SAP session"
                   src={`data:image/jpeg;base64,${frame}`}
                   onClick={onImgClick} onWheel={onWheel}
                   style={{ width: "100%", display: "block", borderRadius: 8, cursor: "crosshair" }} />
            ) : (
              <div className="shimmer" style={{ height: 420, borderRadius: 8 }} />
            )}
            <div style={{ fontSize: 11.5, color: "var(--text-3)", marginTop: 8 }}>
              Click directly on the screen above. Click this panel first, then type —
              keystrokes go to SAP. Enter and F-keys are recorded as SAP virtual keys.
            </div>
          </div>

          <div className="panel" style={{ padding: "14px 16px", borderRadius: 14,
                                           maxHeight: 620, overflowY: "auto" }}>
            <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 10 }}>Captured steps</div>
            {steps.length === 0 && (
              <div className="rsub">Nothing yet — interact with the session and steps appear here.</div>
            )}
            {steps.map((s, i) => (
              <div key={i} style={{ padding: "7px 0", borderBottom: "1px solid var(--hairline)" }}>
                <div style={{ fontSize: 12, fontWeight: 600 }}>
                  <span style={{ color: "var(--blue)" }}>{s.act}</span>{" "}
                  <span style={{ color: "var(--text-2)", fontWeight: 400 }}>{s.target}</span>
                </div>
                {s.value !== null && s.value !== undefined && String(s.value) !== "" && (
                  <div style={{ fontSize: 11.5, color: "var(--text-3)" }}>= {String(s.value)}</div>
                )}
                <div style={{ fontSize: 10.5, color: "var(--text-3)" }}>via {s.resolved_by}</div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
