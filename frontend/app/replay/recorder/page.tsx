"use client";

/* Recorder — drive a server-side SAP session and capture it as a flow (D-033).

   The browser runs HEADLESS in the backend container; what you see here is a
   live JPEG screencast of it, and your clicks/keystrokes are forwarded back
   into it over CDP. That is what makes recording possible in a container —
   the previous design needed a real display and was disabled with a 501. */

import { useCallback, useEffect, useRef, useState } from "react";
import { apiBase } from "../../lib/api";

const API = apiBase();

type SystemRow = { sid: string; system_name?: string; release_tag: string; auth: string };
type Step = { act: string; target: string; value: unknown; resolved_by: string; sap_id?: string | null };
// D-053: eCATT-style steps added by picking an element on the screen.
type PickKind = "get" | "check_field" | "check_rows";
const PICK_HELP: Record<PickKind, string> = {
  get: "Click the field whose value you want to save as a variable",
  check_field: "Click the field whose current value the replay must find",
  check_rows: "Click inside the list that must not be empty",
};

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
  const [tcode, setTcode] = useState("");          // D-034: start transaction
  const [saved, setSaved] = useState<string | null>(null);
  const [pick, setPick] = useState<PickKind | null>(null);   // D-053
  const [varName, setVarName] = useState("");
  const [pattern, setPattern] = useState("");
  const [note, setNote] = useState("");
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
        body: JSON.stringify({ sid, tcode: tcode.trim().toUpperCase() }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
      const j = await res.json();
      setRecId(j.recording_id);
      if (j.viewport) setViewport(j.viewport);
      setFlowId(prev => prev || (tcode
        ? `${tcode.trim().toLowerCase()}_check` : `${sid.toLowerCase()}_flow`));
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

  const capture = async (pages = 0) => {
    if (!recId) return;
    try {
      const res = await fetch(`${API}/api/recorder/capture`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ recording_id: recId, pages }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const j = await res.json();
      if (j.steps) setSteps(j.steps);
    } catch (err) { setError(`Could not add screenshot step: ${err}`); }
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
  const toViewport = (e: React.MouseEvent<HTMLImageElement>) => {
    const img = imgRef.current;
    if (!img) return null;
    const r = img.getBoundingClientRect();
    return { x: Math.round(((e.clientX - r.left) / r.width) * viewport.width),
             y: Math.round(((e.clientY - r.top) / r.height) * viewport.height) };
  };

  // D-053: a browser fires click, click, dblclick for a double-click. Single
  // clicks wait briefly so a double-click is sent to SAP as ONE double-click.
  const clickTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pickRef = useRef<PickKind | null>(null);    // guards against a 2nd pick in flight
  useEffect(() => { pickRef.current = pick; }, [pick]);
  const onImgClick = (e: React.MouseEvent<HTMLImageElement>) => {
    if (e.detail > 1) return;               // part of a double-click
    const p = toViewport(e);
    if (!p) return;
    const k = pickRef.current;
    if (k) { pickRef.current = null; setPick(null); void doPick(k, p.x, p.y); return; }
    if (clickTimer.current) clearTimeout(clickTimer.current);
    clickTimer.current = setTimeout(() => {
      clickTimer.current = null;
      void send([{ kind: "click", ...p }]);
    }, 260);
  };
  const onImgDoubleClick = (e: React.MouseEvent<HTMLImageElement>) => {
    if (pick || pickRef.current) return;
    const p = toViewport(e);
    if (!p) return;
    if (clickTimer.current) {
      clearTimeout(clickTimer.current); clickTimer.current = null;
      void send([{ kind: "dblclick", ...p }]);
    } else {
      // the first click already went to SAP — send only the second half,
      // so SAP sees one double-click, not three clicks
      void send([{ kind: "dblclick", second_only: true, ...p }]);
    }
  };

  const postStep = async (path: string, body: Record<string, unknown>) => {
    setError(""); setNote("");
    try {
      const res = await fetch(`${API}/api/recorder/${path}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ recording_id: recId, ...body }),
      });
      const j = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(j.detail || `HTTP ${res.status}`);
      if (j.steps) setSteps(j.steps);
      return j;
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      return null;
    }
  };

  const doPick = async (kind: PickKind, x: number, y: number) => {
    const j = await postStep("pick", { kind, x, y, var: varName.trim(), pattern: pattern.trim() });
    if (j?.picked) setNote(kind === "get"
      ? `Saved as {{${varName.trim()}}} — recorded value ${JSON.stringify(j.picked.value)}`
      : `Check added — recorded value ${JSON.stringify(j.picked.value)}`);
  };

  const messageStep = async (kind: "get_message" | "check_message") => {
    const j = await postStep("message", { kind, var: varName.trim(), pattern: pattern.trim() });
    if (j?.message) setNote(kind === "get_message"
      ? `Message saved as {{${varName.trim()}}}: ${j.message.text}`
      : `Check added — message type ${j.message.type || "(none)"}: ${j.message.text}`);
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (!recId) return;
    // typing into this page's own inputs (variable name, pattern) is not SAP input
    const t = e.target as HTMLElement;
    if (t.closest && t.closest("input,textarea,select,button")) return;
    e.preventDefault();
    const altGr = e.getModifierState && e.getModifierState("AltGraph");
    if (e.key.length === 1 && (altGr || (e.ctrlKey && e.altKey))) {
      void send([{ kind: "text", text: e.key }]);   // @ \ { } on European keyboards
    } else if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
      void send([{ kind: "text", text: e.key }]);
    } else {
      // D-053: modifiers travel too (CDP bitmask) — Shift+F5, Ctrl+S.
      const modifiers = (e.altKey ? 1 : 0) | (e.ctrlKey || e.metaKey ? 2 : 0) | (e.shiftKey ? 8 : 0);
      void send([{ kind: "key", key: e.key, code: e.code, modifiers }]);
    }
  };

  // D-035: scrolling the remote page.
  // React's onWheel is registered PASSIVE, so preventDefault() is ignored and
  // the wheel scrolled this page instead of the SAP session — scrolling
  // simply didn't work. A non-passive native listener fixes that. Deltas are
  // also amplified: SAP's scroll containers ignore very small deltas.
  useEffect(() => {
    const img = imgRef.current;
    if (!img || !recId) return;
    const handler = (e: WheelEvent) => {
      e.preventDefault();
      const r = img.getBoundingClientRect();
      void send([{
        kind: "wheel",
        x: Math.round(((e.clientX - r.left) / r.width) * viewport.width),
        y: Math.round(((e.clientY - r.top) / r.height) * viewport.height),
        dx: e.deltaX, dy: e.deltaY * 3,
      }]);
    };
    img.addEventListener("wheel", handler, { passive: false });
    return () => img.removeEventListener("wheel", handler);
  }, [recId, frame, viewport, send]);

  // Keyboard scrolling as a reliable alternative to the wheel.
  const scrollBy = (dy: number) => void send([
    { kind: "wheel", x: Math.round(viewport.width / 2),
      y: Math.round(viewport.height / 2), dx: 0, dy },
  ]);

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
          ✓ {saved} — run it from <a href="/replay" style={{ color: "inherit", textDecoration: "underline" }}>Record and replay</a> with &quot;Functional flows&quot; ticked.
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
            <label style={{ fontSize: 12.5, color: "var(--text-2)" }}>Start at T-code</label>
            <input className="input" value={tcode}
                   onChange={e => setTcode(e.target.value.toUpperCase())}
                   placeholder="SM37" style={{ width: 120 }}
                   title="The session jumps here before recording starts, and this becomes step 1 of the flow" />
            <button className="btn" disabled={!sid || busy} onClick={start}>
              {busy ? "Starting…" : "Start Recording"}
            </button>
            <span style={{ fontSize: 11.5, color: "var(--text-3)" }}>
              Leave T-code blank to start from the SAP home screen.
            </span>
          </>
        ) : (
          <>
            <span className="pulse" style={{ width: 9, height: 9, borderRadius: "50%",
                                              background: "var(--red)" }} />
            <span style={{ fontSize: 13, fontWeight: 600 }}>Recording {sid}</span>
            <span style={{ fontSize: 12, color: "var(--text-3)" }}>{steps.length} steps</span>
            <button className="btn secondary" disabled={busy} onClick={() => capture(0)}
                    title="Insert a screenshot step here — it appears as evidence in the report">
              📸 Capture screen
            </button>
            <button className="btn secondary" disabled={busy} onClick={() => capture(25)}
                    title="On replay, scroll the list a page at a time and screenshot every page (stops automatically at the end, max 25)">
              📄 Capture all pages
            </button>
            <label style={{ fontSize: 12.5, color: "var(--text-2)", marginLeft: 6 }}>Name</label>
            <input className="input" value={flowId}
                   onChange={e => setFlowId(e.target.value.replace(/[^A-Za-z0-9_.-]/g, "_"))}
                   placeholder="sm37_cancelled_jobs" style={{ width: 200 }} />
            <button className="btn" disabled={busy || !flowId.trim()} onClick={stop}
                    style={{ marginLeft: "auto" }}>
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
                   onClick={onImgClick}
                   onDoubleClick={onImgDoubleClick}
                   style={{ width: "100%", display: "block", borderRadius: 8,
                            cursor: pick ? "cell" : "crosshair",
                            outline: pick ? "2px solid var(--orange)" : "none" }} />
            ) : (
              <div className="shimmer" style={{ height: 420, borderRadius: 8 }} />
            )}
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 8, flexWrap: "wrap" }}>
              <span style={{ fontSize: 11.5, color: "var(--text-3)" }}>Scroll:</span>
              <button className="btn secondary" style={{ fontSize: 11.5, padding: "3px 10px" }}
                      onClick={() => scrollBy(-400)}>↑ Up</button>
              <button className="btn secondary" style={{ fontSize: 11.5, padding: "3px 10px" }}
                      onClick={() => scrollBy(400)}>↓ Down</button>
              <button className="btn secondary" style={{ fontSize: 11.5, padding: "3px 10px" }}
                      onClick={() => capture(0)} title="Insert a screenshot step at this scroll position">
                📸 Capture this view
              </button>
              <button className="btn secondary" style={{ fontSize: 11.5, padding: "3px 10px" }}
                      onClick={() => capture(25)}
                      title="Scroll from here to the end at replay time, capturing every page">
                📄 Capture all pages from here
              </button>
              <span style={{ fontSize: 11.5, color: "var(--text-3)" }}>
                — or use your mouse wheel over the screen.
              </span>
            </div>
            {/* D-053: eCATT-style variables and checks */}
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 10, flexWrap: "wrap",
                          paddingTop: 10, borderTop: "1px solid var(--hairline)" }}>
              <span style={{ fontSize: 11.5, color: "var(--text-3)" }}>Variable</span>
              <input className="input" value={varName} onChange={e => setVarName(e.target.value)}
                     placeholder="po_number" style={{ width: 120, fontSize: 11.5, padding: "3px 8px" }}
                     title="Name used later as {{po_number}} in field values or the T-code" />
              <span style={{ fontSize: 11.5, color: "var(--text-3)" }}>Pattern</span>
              <input className="input" value={pattern} onChange={e => setPattern(e.target.value)}
                     placeholder="(\d+)  optional" style={{ width: 110, fontSize: 11.5, padding: "3px 8px" }}
                     title="Optional regex; its first group (or the whole match) becomes the value" />
              <button className="btn secondary" style={{ fontSize: 11.5, padding: "3px 10px" }}
                      disabled={!varName.trim()} onClick={() => setPick(pick === "get" ? null : "get")}>
                ⤓ Save field value
              </button>
              <button className="btn secondary" style={{ fontSize: 11.5, padding: "3px 10px" }}
                      disabled={!varName.trim()} onClick={() => messageStep("get_message")}>
                ⤓ Save status message
              </button>
              <button className="btn secondary" style={{ fontSize: 11.5, padding: "3px 10px" }}
                      onClick={() => setPick(pick === "check_field" ? null : "check_field")}>
                ✓ Check field value
              </button>
              <button className="btn secondary" style={{ fontSize: 11.5, padding: "3px 10px" }}
                      onClick={() => setPick(pick === "check_rows" ? null : "check_rows")}>
                ✓ Check list has rows
              </button>
              <button className="btn secondary" style={{ fontSize: 11.5, padding: "3px 10px" }}
                      onClick={() => messageStep("check_message")}>
                ✓ Check status message type
              </button>
            </div>
            {pick && (
              <div style={{ fontSize: 12, color: "var(--orange)", marginTop: 6 }}>
                {PICK_HELP[pick]} — nothing is clicked in SAP.{" "}
                <span style={{ cursor: "pointer", textDecoration: "underline" }} onClick={() => setPick(null)}>cancel</span>
              </div>
            )}
            {note && <div style={{ fontSize: 12, color: "var(--green)", marginTop: 6 }}>✓ {note}</div>}
            <div style={{ fontSize: 11.5, color: "var(--text-3)", marginTop: 6 }}>
              Click directly on the screen above. Click this panel first, then type —
              keystrokes go to SAP. Enter, Esc, Ctrl+S and (Shift/Ctrl+) F-keys are recorded as SAP
              virtual keys; double-click works too.
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
