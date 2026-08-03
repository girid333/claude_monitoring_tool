"use client";

/* D-045: RFC Checks — BASIS monitoring via SAP's RFC-enabled function
   modules over /sap/bc/soap/rfc.

   No recording, no replay, no browser automation, no workstation agent:
   this talks to the same host as WebGUI using the same stored credential,
   so a system that's already on the Systems page needs no extra setup.

   Deliberate UI choice (the D-044 lesson): a check that FAILS is rendered
   as prominently as one that succeeds, with SAP's own error text and the
   function module name. Nothing here ever shows an empty table that could
   be mistaken for "all clear". */

import { useEffect, useState } from "react";
import { apiBase } from "../lib/api";

const API = apiBase();

type SystemRow = { sid: string; system_name?: string };
type CheckMeta = {
  check_id: string; title: string; tcode: string;
  function: string; confidence: string; note: string;
};
type CheckResult = {
  check_id: string; title: string; ok: boolean;
  rows: Record<string, string>[]; anomalies: string[];
  summary: string; error: string; error_kind: string; function: string;
  suspicious?: string;
};
type Conn = {
  ok: boolean; sid: string; endpoint: string; system?: string; host?: string;
  release?: string; database?: string; kernel?: string; os?: string;
  error?: string; error_kind?: string;
};

export default function RfcPage() {
  const [systems, setSystems] = useState<SystemRow[]>([]);
  const [sid, setSid] = useState("");
  const [checks, setChecks] = useState<CheckMeta[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [conn, setConn] = useState<Conn | null>(null);
  const [results, setResults] = useState<CheckResult[]>([]);
  const [anomalyCount, setAnomalyCount] = useState(0);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    fetch(`${API}/api/systems`).then(r => r.json()).then((s: SystemRow[]) => {
      setSystems(s);
      if (s.length) setSid(prev => prev || s[0].sid);
    }).catch(err => setError(`Could not reach the backend at ${API}: ${err}`));
    fetch(`${API}/api/rfc/checks`).then(r => r.json()).then((c: CheckMeta[]) => {
      setChecks(c);
      setSelected(new Set(c.map(x => x.check_id)));
    }).catch(() => {});
  }, []);

  const testConnection = async () => {
    setBusy("test"); setError(""); setConn(null); setResults([]);
    try {
      const r = await fetch(`${API}/api/rfc/test-connection`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sid }),
      });
      const j = await r.json();
      if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
      setConn(j);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally { setBusy(""); }
  };

  const runChecks = async () => {
    setBusy("run"); setError(""); setResults([]);
    try {
      const all = selected.size === checks.length;
      const r = await fetch(`${API}/api/rfc/run`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sid, check_ids: all ? null : Array.from(selected) }),
      });
      const j = await r.json();
      if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
      setResults(j.checks || []);
      setAnomalyCount(j.anomaly_count || 0);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally { setBusy(""); }
  };

  const toggle = (id: string) => setSelected(prev => {
    const next = new Set(prev);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });

  return (
    <div style={{ maxWidth: 1200 }} className="page-enter">
      <h1 className="ltitle">RFC Checks</h1>
      <p className="lsub">
        Reads BASIS data straight from SAP&apos;s RFC-enabled function modules over
        <code style={{ margin: "0 4px" }}>/sap/bc/soap/rfc</code> — no screen recording, no
        replay, no workstation agent, nothing to install. Uses the same host and stored
        credential as the system&apos;s WebGUI entry, so anything on the Systems page works
        here with no extra setup.
      </p>

      {error && (
        <div style={{ margin: "0 0 16px", padding: "10px 14px", borderRadius: 10,
                       border: "1px solid var(--red)", color: "var(--red)", fontSize: 12.5 }}>
          {error}
        </div>
      )}

      {/* ── Controls ─────────────────────────────────────────────────── */}
      <div className="panel" style={{ padding: "14px 18px", marginBottom: 18, borderRadius: 14,
                                       display: "flex", gap: 14, alignItems: "center", flexWrap: "wrap" }}>
        <label style={{ fontSize: 12.5, color: "var(--text-2)" }}>System</label>
        <select className="input" value={sid} onChange={e => setSid(e.target.value)}
                style={{ minWidth: 220 }}>
          {systems.length === 0 && <option value="">No systems — add one first</option>}
          {systems.map(s => (
            <option key={s.sid} value={s.sid}>
              {s.system_name ? `${s.system_name} (${s.sid})` : s.sid}
            </option>
          ))}
        </select>
        <button className="btn secondary" disabled={!sid || !!busy} onClick={testConnection}>
          {busy === "test" ? "Testing…" : "Test connection"}
        </button>
        <button className="btn" disabled={!sid || !!busy || selected.size === 0} onClick={runChecks}>
          {busy === "run" ? "Running…" : `Run ${selected.size} check${selected.size === 1 ? "" : "s"}`}
        </button>
      </div>

      {/* ── Connection result ────────────────────────────────────────── */}
      {conn && (
        <div className="panel" style={{
          padding: "12px 18px", marginBottom: 18, borderRadius: 14,
          border: `1px solid ${conn.ok ? "var(--green)" : "var(--red)"}`,
        }}>
          {conn.ok ? (
            <>
              <div style={{ fontSize: 13, fontWeight: 600, color: "var(--green)", marginBottom: 6 }}>
                ✓ Connected to {conn.system}
              </div>
              <div style={{ fontSize: 12, color: "var(--text-2)", display: "flex",
                             gap: 18, flexWrap: "wrap" }}>
                <span>Host: {conn.host}</span>
                <span>Release: {conn.release}</span>
                <span>Kernel: {conn.kernel}</span>
                <span>DB: {conn.database}</span>
                <span>OS: {conn.os}</span>
              </div>
            </>
          ) : (
            <>
              <div style={{ fontSize: 13, fontWeight: 600, color: "var(--red)", marginBottom: 6 }}>
                ✗ Could not connect ({conn.error_kind})
              </div>
              <div style={{ fontSize: 12, color: "var(--text-2)" }}>{conn.error}</div>
            </>
          )}
          <div style={{ fontSize: 11, color: "var(--text-3)", marginTop: 6 }}>
            Endpoint: {conn.endpoint}
          </div>
        </div>
      )}

      {/* ── Check picker ─────────────────────────────────────────────── */}
      <div className="panel" style={{ padding: "14px 18px", marginBottom: 18, borderRadius: 14 }}>
        <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 10 }}>
          Checks ({selected.size}/{checks.length} selected)
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(280px,1fr))",
                       gap: 8 }}>
          {checks.map(c => (
            <label key={c.check_id} style={{
              display: "flex", gap: 8, alignItems: "flex-start", padding: "8px 10px",
              borderRadius: 8, border: "1px solid var(--hairline)", cursor: "pointer",
              background: selected.has(c.check_id) ? "rgba(10,132,255,.08)" : "transparent",
            }}>
              <input type="checkbox" checked={selected.has(c.check_id)}
                     onChange={() => toggle(c.check_id)} style={{ marginTop: 2 }} />
              <span>
                <span style={{ fontSize: 12.5, fontWeight: 600 }}>{c.title}</span>
                {c.tcode !== "—" && (
                  <span style={{ fontSize: 11, color: "var(--blue)", marginLeft: 6 }}>{c.tcode}</span>
                )}
                {c.confidence === "unverified" && (
                  <span title={c.note} style={{
                    fontSize: 10, marginLeft: 6, padding: "1px 5px", borderRadius: 4,
                    background: "rgba(255,159,10,.16)", color: "var(--orange,#FF9F0A)",
                  }}>unverified</span>
                )}
                <span style={{ display: "block", fontSize: 10.5, color: "var(--text-3)" }}>
                  {c.function}
                </span>
              </span>
            </label>
          ))}
        </div>
      </div>

      {/* ── Results ──────────────────────────────────────────────────── */}
      {results.length > 0 && (
        <div style={{ marginBottom: 10, fontSize: 13 }}>
          <strong>{results.filter(r => r.ok).length}/{results.length}</strong> checks succeeded
          {anomalyCount > 0 && (
            <span style={{ color: "var(--red)", marginLeft: 10 }}>
              · {anomalyCount} anomal{anomalyCount === 1 ? "y" : "ies"} found
            </span>
          )}
        </div>
      )}

      {results.map(r => (
        <div key={r.check_id} className="panel" style={{
          padding: "14px 18px", marginBottom: 14, borderRadius: 14,
          borderLeft: `3px solid ${!r.ok ? "var(--red)"
            : r.anomalies.length ? "var(--red)"
            : r.suspicious ? "var(--orange,#FF9F0A)" : "var(--green)"}`,
        }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
            <span style={{ fontSize: 13.5, fontWeight: 600 }}>{r.title}</span>
            <span style={{ fontSize: 11, color: "var(--text-3)" }}>{r.function}</span>
            {r.ok
              ? <span style={{ fontSize: 12, color: "var(--text-2)", marginLeft: "auto" }}>{r.summary}</span>
              : <span style={{ fontSize: 12, color: "var(--red)", marginLeft: "auto" }}>
                  failed ({r.error_kind})
                </span>}
          </div>

          {/* A failed check is loud and specific — never an empty table. */}
          {!r.ok && (
            <div style={{ marginTop: 8, padding: "8px 10px", borderRadius: 8,
                           background: "rgba(255,69,58,.10)", color: "var(--red)", fontSize: 12 }}>
              {r.error}
            </div>
          )}

          {r.anomalies.length > 0 && (
            <ul style={{ margin: "8px 0 0", paddingLeft: 18, fontSize: 12, color: "var(--red)" }}>
              {r.anomalies.map((a, i) => <li key={i} style={{ padding: "1px 0" }}>{a}</li>)}
            </ul>
          )}

          {r.ok && r.rows.length > 0 && (
            <div style={{ marginTop: 10, overflowX: "auto" }}>
              <table style={{ width: "100%", fontSize: 11.5, borderCollapse: "collapse" }}>
                <thead>
                  <tr style={{ color: "var(--text-3)", textAlign: "left" }}>
                    {Object.keys(r.rows[0]).map(k => (
                      <th key={k} style={{ padding: "4px 8px", whiteSpace: "nowrap" }}>{k}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {r.rows.slice(0, 100).map((row, i) => (
                    <tr key={i} style={{ borderTop: "1px solid var(--hairline)" }}>
                      {Object.keys(r.rows[0]).map(k => (
                        <td key={k} style={{ padding: "4px 8px", whiteSpace: "nowrap" }}>{row[k]}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
              {r.rows.length > 100 && (
                <div style={{ fontSize: 11, color: "var(--text-3)", marginTop: 6 }}>
                  showing first 100 of {r.rows.length} rows
                </div>
              )}
            </div>
          )}

          {/* A call that succeeds but returns nothing is NOT reassuring — for
              these checks it almost always means we parsed the wrong field or
              lack authorization. Say so plainly. */}
          {r.ok && r.suspicious && (
            <div style={{ marginTop: 8, padding: "8px 10px", borderRadius: 8,
                           background: "rgba(255,159,10,.12)",
                           color: "var(--orange,#FF9F0A)", fontSize: 12 }}>
              ⚠ {r.suspicious}
            </div>
          )}

          {r.ok && !r.suspicious && r.rows.length === 0 && (
            <div style={{ marginTop: 8, fontSize: 12, color: "var(--text-3)" }}>
              Call succeeded and returned no rows.
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
