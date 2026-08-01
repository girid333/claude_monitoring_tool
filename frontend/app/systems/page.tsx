"use client";

/* Systems — CRUD over the registry (D-021). Replaces hand-editing
   systems.json, per Giri's request. Mirrors v1's proven table+modal UX
   (sap_monitoring/frontend/src/app/page.tsx "System Inventory" view) but
   deliberately drops the plaintext credential fields v1 stored in SQLite —
   D-018 already settled that credentials only ever live in env vars on the
   machine running the backend. This page shows a configured/missing badge
   per system instead, with the exact `export` command to run. */

import { useEffect, useState } from "react";

const API = process.env.NEXT_PUBLIC_API ?? "http://localhost:8000";

type SystemEntry = {
  sid: string; webgui_url: string; client: string; release_tag: string;
  auth: string; tags: string[]; rfc_enrichment: boolean;
  hostname: string; instance_number: string; db_host: string;
  servers: string[]; url_checks: string[]; ssh_auth_method: string;
};
type SystemRow = SystemEntry & {
  last_run: unknown;
  credentials: { sap_credentials: boolean; ssh_credentials: boolean };
};

const EMPTY: SystemEntry = {
  sid: "", webgui_url: "", client: "", release_tag: "UNKNOWN", auth: "basic",
  tags: [], rfc_enrichment: false, hostname: "", instance_number: "00",
  db_host: "", servers: [], url_checks: [], ssh_auth_method: "password",
};

export default function Systems() {
  const [systems, setSystems] = useState<SystemRow[]>([]);
  const [modalOpen, setModalOpen] = useState(false);
  const [editingSid, setEditingSid] = useState<string | null>(null);
  const [form, setForm] = useState<SystemEntry>(EMPTY);
  const [tagInput, setTagInput] = useState("");
  const [serverInput, setServerInput] = useState("");
  const [urlInput, setUrlInput] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [hint, setHint] = useState<{ sap: string; ssh: string } | null>(null);

  const load = () => fetch(`${API}/api/systems`).then(r => r.json()).then(setSystems).catch(() => {});
  useEffect(() => { load(); }, []);

  const openAdd = () => { setEditingSid(null); setForm(EMPTY); setError(null); setModalOpen(true); };
  const openEdit = (s: SystemRow) => {
    setEditingSid(s.sid);
    setForm({ ...s });
    setError(null);
    setModalOpen(true);
  };

  const save = async () => {
    setError(null);
    const method = editingSid ? "PUT" : "POST";
    const url = editingSid ? `${API}/api/systems/${editingSid}` : `${API}/api/systems`;
    const res = await fetch(url, {
      method, headers: { "Content-Type": "application/json" },
      body: JSON.stringify(form),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      setError(body.detail ?? `save failed (${res.status})`);
      return;
    }
    setModalOpen(false);
    load();
  };

  const remove = async (sid: string) => {
    if (!confirm(`Delete system ${sid}? This only removes it from the registry — it does not touch flow/report history.`)) return;
    await fetch(`${API}/api/systems/${sid}`, { method: "DELETE" });
    load();
  };

  const showCredHint = async (sid: string) => {
    const res = await fetch(`${API}/api/systems/${sid}/credentials`);
    if (res.ok) setHint((await res.json()).hint);
  };

  return (
    <div style={{ maxWidth: 900 }}>
      <h1 className="ltitle">Systems</h1>
      <p className="lsub">
        <b>{systems.length} systems</b> in the registry (D-006/D-021) — credentials
        are never stored here, only connection topology. Set credentials as
        environment variables on the machine running the backend.
      </p>

      <div style={{ marginBottom: 16 }}>
        <button className="btn" onClick={openAdd}>+ Add System</button>
      </div>

      <div className="list">
        {systems.length === 0 && (
          <div className="row"><span className="rsub">No systems yet — click &quot;Add System&quot;.</span></div>
        )}
        {systems.map(s => (
          <div className="row" key={s.sid}>
            <div className="grow">
              <div className="rtitle">{s.sid} <span style={{ color: "var(--text-3)", fontWeight: 400 }}>
                · {s.release_tag} · {s.auth}</span></div>
              <div className="rsub">{s.webgui_url}</div>
              <div style={{ marginTop: 6 }}>
                <span className={s.credentials.sap_credentials ? "badge-ok" : "badge-missing"}>
                  {s.credentials.sap_credentials ? "● SAP credentials set" : "○ SAP credentials missing"}
                </span>
                {(s.servers.length > 0 || s.db_host) && (
                  <span className={s.credentials.ssh_credentials ? "badge-ok" : "badge-missing"} style={{ marginLeft: 12 }}>
                    {s.credentials.ssh_credentials ? "● SSH credentials set" : "○ SSH credentials missing"}
                  </span>
                )}
                {!s.credentials.sap_credentials && (
                  <button className="btn secondary" style={{ marginLeft: 10, padding: "2px 8px", fontSize: 10.5 }}
                          onClick={() => showCredHint(s.sid)}>show export command</button>
                )}
              </div>
            </div>
            <button className="btn secondary" onClick={() => openEdit(s)}>Edit</button>
            <button className="btn danger" onClick={() => remove(s.sid)}>Delete</button>
          </div>
        ))}
      </div>

      {hint && (
        <div className="panel" style={{ padding: 16, marginTop: 16, fontFamily: "monospace", fontSize: 12 }}>
          <div>{hint.sap}</div>
          {hint.ssh && <div style={{ marginTop: 4 }}>{hint.ssh}</div>}
          <button className="btn secondary" style={{ marginTop: 10 }} onClick={() => setHint(null)}>Close</button>
        </div>
      )}

      {modalOpen && (
        <div className="modal-overlay" onClick={() => setModalOpen(false)}>
          <div className="modal-content" onClick={e => e.stopPropagation()}>
            <h2 style={{ marginBottom: 16 }}>{editingSid ? `Edit ${editingSid}` : "Add System"}</h2>

            {error && <div style={{ color: "var(--red)", fontSize: 12.5, marginBottom: 12 }}>{error}</div>}

            <div className="field-row">
              <div className="field-group">
                <label className="field-label">SID</label>
                <input className="input" style={{ width: "100%" }} value={form.sid} disabled={!!editingSid}
                       onChange={e => setForm({ ...form, sid: e.target.value.toUpperCase() })} placeholder="Q4H" />
              </div>
              <div className="field-group">
                <label className="field-label">Client</label>
                <input className="input" style={{ width: "100%" }} value={form.client}
                       onChange={e => setForm({ ...form, client: e.target.value })} placeholder="100" />
              </div>
            </div>

            <div className="field-group">
              <label className="field-label">WebGUI URL</label>
              <input className="input" style={{ width: "100%" }} value={form.webgui_url}
                     onChange={e => setForm({ ...form, webgui_url: e.target.value })}
                     placeholder="https://host:port/sap/bc/gui/sap/its/webgui/" />
            </div>

            <div className="field-row">
              <div className="field-group">
                <label className="field-label">Release tag</label>
                <input className="input" style={{ width: "100%" }} value={form.release_tag}
                       onChange={e => setForm({ ...form, release_tag: e.target.value })} placeholder="S4_2023" />
              </div>
              <div className="field-group">
                <label className="field-label">Login plugin</label>
                <select className="input" style={{ width: "100%" }} value={form.auth}
                        onChange={e => setForm({ ...form, auth: e.target.value })}>
                  <option value="basic">basic</option>
                  <option value="saml">saml</option>
                </select>
              </div>
            </div>

            <div className="field-group">
              <label className="field-label">Tags</label>
              <div>
                {form.tags.map(t => (
                  <span className="chip" key={t}>{t}
                    <button onClick={() => setForm({ ...form, tags: form.tags.filter(x => x !== t) })}>×</button>
                  </span>
                ))}
              </div>
              <div style={{ display: "flex", gap: 8 }}>
                <input className="input" style={{ flex: 1 }} value={tagInput}
                       onChange={e => setTagInput(e.target.value)}
                       onKeyDown={e => { if (e.key === "Enter" && tagInput) { setForm({ ...form, tags: [...form.tags, tagInput] }); setTagInput(""); } }}
                       placeholder="reference, press Enter" />
              </div>
            </div>

            <label style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12.5, marginBottom: 14 }}>
              <input type="checkbox" checked={form.rfc_enrichment}
                     onChange={e => setForm({ ...form, rfc_enrichment: e.target.checked })} />
              RFC enrichment (D-013: optional, never blocks the run)
            </label>

            <h3 style={{ fontSize: 12.5, color: "var(--text-2)", margin: "16px 0 10px", borderTop: "1px solid var(--hairline)", paddingTop: 14 }}>
              Core BASIS connection topology (D-021, all non-secret)
            </h3>

            <div className="field-row">
              <div className="field-group">
                <label className="field-label">RFC hostname (blank = derive from WebGUI URL)</label>
                <input className="input" style={{ width: "100%" }} value={form.hostname}
                       onChange={e => setForm({ ...form, hostname: e.target.value })} />
              </div>
              <div className="field-group">
                <label className="field-label">Instance number</label>
                <input className="input" style={{ width: "100%" }} value={form.instance_number}
                       onChange={e => setForm({ ...form, instance_number: e.target.value })} placeholder="00" />
              </div>
            </div>

            <div className="field-row">
              <div className="field-group">
                <label className="field-label">DB host (blank = same as hostname)</label>
                <input className="input" style={{ width: "100%" }} value={form.db_host}
                       onChange={e => setForm({ ...form, db_host: e.target.value })} />
              </div>
              <div className="field-group">
                <label className="field-label">SSH auth method</label>
                <select className="input" style={{ width: "100%" }} value={form.ssh_auth_method}
                        onChange={e => setForm({ ...form, ssh_auth_method: e.target.value })}>
                  <option value="password">password</option>
                  <option value="key">key</option>
                  <option value="mfa">mfa</option>
                </select>
              </div>
            </div>

            <div className="field-group">
              <label className="field-label">Additional app/dialog servers (SSH filesystem checks)</label>
              <div>
                {form.servers.map(s => (
                  <span className="chip" key={s}>{s}
                    <button onClick={() => setForm({ ...form, servers: form.servers.filter(x => x !== s) })}>×</button>
                  </span>
                ))}
              </div>
              <input className="input" style={{ width: "100%" }} value={serverInput}
                     onChange={e => setServerInput(e.target.value)}
                     onKeyDown={e => { if (e.key === "Enter" && serverInput) { setForm({ ...form, servers: [...form.servers, serverInput] }); setServerInput(""); } }}
                     placeholder="app_server_2, press Enter" />
            </div>

            <div className="field-group">
              <label className="field-label">Health check URLs</label>
              <div>
                {form.url_checks.map(u => (
                  <span className="chip" key={u}>{u}
                    <button onClick={() => setForm({ ...form, url_checks: form.url_checks.filter(x => x !== u) })}>×</button>
                  </span>
                ))}
              </div>
              <input className="input" style={{ width: "100%" }} value={urlInput}
                     onChange={e => setUrlInput(e.target.value)}
                     onKeyDown={e => { if (e.key === "Enter" && urlInput) { setForm({ ...form, url_checks: [...form.url_checks, urlInput] }); setUrlInput(""); } }}
                     placeholder="https://..., press Enter" />
            </div>

            <div style={{ fontSize: 11, color: "var(--text-3)", margin: "10px 0 16px" }}>
              Credentials aren&apos;t set here — after saving, export
              SAP_{form.sid || "&lt;SID&gt;"}_USER / _PASSWORD (and SSH
              equivalents if needed) on the machine running the backend.
            </div>

            <div style={{ display: "flex", gap: 10, justifyContent: "flex-end" }}>
              <button className="btn secondary" onClick={() => setModalOpen(false)}>Cancel</button>
              <button className="btn" disabled={!form.sid || !form.webgui_url || !form.client} onClick={save}>
                {editingSid ? "Save changes" : "Add system"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
