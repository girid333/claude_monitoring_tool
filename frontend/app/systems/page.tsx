"use client";

/* Systems — CRUD over the registry (D-021, extended D-023). Replaces
   hand-editing systems.json, per Giri's request. Mirrors v1's proven
   table+modal UX (sap_monitoring/frontend/src/app/page.tsx "System
   Inventory" view).

   D-023: SSH credentials (for app/dialog servers + DB hosts, used for the
   `df -h` filesystem checks Core BASIS runs) are now entered and stored
   HERE, encrypted at rest on the backend (engine/crypto.py) — this
   supersedes D-018 for SSH creds specifically, per Giri's explicit
   request. SAP WebGUI login credentials are UNCHANGED: still env-var-only
   (D-018), shown as a configured/missing badge + export-command hint, no
   password field for those.

   Each app/dialog server and DB host can optionally override the system's
   default SSH credential (`ssh_credential`) — matches v1's own
   `use_custom_auth` per-server model (orchestrator.py
   run_ssh_filesystem_checks, confirmed while wiring D-023 into
   core_basis_wrap.build_v1_config). Passwords are never round-tripped: the
   API only ever returns `password_set: boolean`, never a value — leaving
   a password field blank on an edit means "keep what's already stored". */

import { useEffect, useState } from "react";

const API = process.env.NEXT_PUBLIC_API ?? "http://localhost:8000";

type CredentialForm = {
  auth_method: string;   // "password" | "key"
  username: string;
  password: string;      // plaintext to SET; blank on edit = keep existing
  key_path: string;
  password_set: boolean; // display-only, from the API's redacted response
};

const EMPTY_CRED: CredentialForm = {
  auth_method: "password", username: "", password: "", key_path: "", password_set: false,
};

type HostForm = {
  hostname: string;
  override: boolean;         // false = inherit the system's default ssh_credential
  credential: CredentialForm;
};

type SystemForm = {
  sid: string; webgui_url: string; client: string; release_tag: string;
  auth: string; tags: string[]; rfc_enrichment: boolean;
  hostname: string; instance_number: string; url_checks: string[];
  servers: HostForm[];
  db_hosts: HostForm[];
  ssh_credential: CredentialForm;
};

type CredentialOut = { auth_method: string; username: string; key_path: string; password_set: boolean } | null;
type HostOut = { hostname: string; credential: CredentialOut };
type SystemRow = {
  sid: string; webgui_url: string; client: string; release_tag: string;
  auth: string; tags: string[]; rfc_enrichment: boolean;
  hostname: string; instance_number: string; url_checks: string[];
  servers: HostOut[]; db_hosts: HostOut[]; ssh_credential: CredentialOut;
  last_run: unknown;
  credentials: { sap_credentials: boolean; ssh_default_credential: boolean;
                 ssh_hosts: { hostname: string; credential_set: boolean }[] };
};

const EMPTY: SystemForm = {
  sid: "", webgui_url: "", client: "", release_tag: "UNKNOWN", auth: "basic",
  tags: [], rfc_enrichment: false, hostname: "", instance_number: "00",
  url_checks: [], servers: [], db_hosts: [], ssh_credential: { ...EMPTY_CRED },
};

function outToForm(c: CredentialOut): CredentialForm {
  return c
    ? { auth_method: c.auth_method, username: c.username, password: "", key_path: c.key_path, password_set: c.password_set }
    : { ...EMPTY_CRED };
}

function hostOutToForm(h: HostOut): HostForm {
  return { hostname: h.hostname, override: h.credential !== null, credential: outToForm(h.credential) };
}

function rowToForm(s: SystemRow): SystemForm {
  return {
    sid: s.sid, webgui_url: s.webgui_url, client: s.client, release_tag: s.release_tag,
    auth: s.auth, tags: s.tags, rfc_enrichment: s.rfc_enrichment,
    hostname: s.hostname, instance_number: s.instance_number, url_checks: s.url_checks,
    servers: s.servers.map(hostOutToForm),
    db_hosts: s.db_hosts.map(hostOutToForm),
    ssh_credential: outToForm(s.ssh_credential),
  };
}

// Build the POST/PUT body: strips display-only `password_set`, drops the
// credential override entirely when `override` is false (server-side reads
// that as "inherit the system default").
function formToBody(f: SystemForm) {
  const cred = (c: CredentialForm) => ({
    auth_method: c.auth_method, username: c.username, password: c.password, key_path: c.key_path,
  });
  const host = (h: HostForm) => ({ hostname: h.hostname, credential: h.override ? cred(h.credential) : null });
  return {
    sid: f.sid, webgui_url: f.webgui_url, client: f.client, release_tag: f.release_tag,
    auth: f.auth, tags: f.tags, rfc_enrichment: f.rfc_enrichment,
    hostname: f.hostname, instance_number: f.instance_number, url_checks: f.url_checks,
    servers: f.servers.map(host), db_hosts: f.db_hosts.map(host),
    ssh_auth_method: f.ssh_credential.auth_method, ssh_credential: cred(f.ssh_credential),
  };
}

function CredentialFields({ cred, onChange }: { cred: CredentialForm; onChange: (c: CredentialForm) => void }) {
  return (
    <div style={{ padding: "10px 12px", background: "rgba(255,255,255,.03)", borderRadius: "var(--r-sm)", marginTop: 6 }}>
      <div className="field-row">
        <div className="field-group">
          <label className="field-label">Auth method</label>
          <select className="input" style={{ width: "100%" }} value={cred.auth_method}
                  onChange={e => onChange({ ...cred, auth_method: e.target.value })}>
            <option value="password">password</option>
            <option value="key">key</option>
          </select>
        </div>
        <div className="field-group">
          <label className="field-label">SSH username</label>
          <input className="input" style={{ width: "100%" }} value={cred.username}
                 onChange={e => onChange({ ...cred, username: e.target.value })} />
        </div>
      </div>
      {cred.auth_method === "key" ? (
        <div className="field-group">
          <label className="field-label">Key file path (on the backend host)</label>
          <input className="input" style={{ width: "100%" }} value={cred.key_path}
                 onChange={e => onChange({ ...cred, key_path: e.target.value })}
                 placeholder="/etc/sap-agent/keys/id_rsa" />
        </div>
      ) : (
        <div className="field-group">
          <label className="field-label">SSH password</label>
          <input className="input" style={{ width: "100%" }} type="password" value={cred.password}
                 onChange={e => onChange({ ...cred, password: e.target.value })}
                 placeholder={cred.password_set ? "•••••••• (leave blank to keep)" : "not set"} />
        </div>
      )}
    </div>
  );
}

function HostList({ label, hosts, setHosts }: {
  label: string; hosts: HostForm[]; setHosts: (h: HostForm[]) => void;
}) {
  const [input, setInput] = useState("");
  const add = () => {
    if (!input) return;
    setHosts([...hosts, { hostname: input, override: false, credential: { ...EMPTY_CRED } }]);
    setInput("");
  };
  const update = (i: number, patch: Partial<HostForm>) =>
    setHosts(hosts.map((h, idx) => (idx === i ? { ...h, ...patch } : h)));
  const remove = (i: number) => setHosts(hosts.filter((_, idx) => idx !== i));

  return (
    <div className="field-group">
      <label className="field-label">{label}</label>
      {hosts.map((h, i) => (
        <div key={i} style={{ marginBottom: 8, padding: "8px 10px", border: "1px solid var(--hairline)", borderRadius: "var(--r-sm)" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span style={{ flex: 1, fontSize: 12.5 }}>{h.hostname}</span>
            <label style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 11, color: "var(--text-2)" }}>
              <input type="checkbox" checked={h.override} onChange={e => update(i, { override: e.target.checked })} />
              own SSH credential
            </label>
            <button className="btn danger" style={{ padding: "2px 8px", fontSize: 10.5 }} onClick={() => remove(i)}>×</button>
          </div>
          {h.override && (
            <CredentialFields cred={h.credential} onChange={c => update(i, { credential: c })} />
          )}
        </div>
      ))}
      <div style={{ display: "flex", gap: 8 }}>
        <input className="input" style={{ flex: 1 }} value={input} onChange={e => setInput(e.target.value)}
               onKeyDown={e => { if (e.key === "Enter") add(); }} placeholder="hostname, press Enter" />
      </div>
    </div>
  );
}

export default function Systems() {
  const [systems, setSystems] = useState<SystemRow[]>([]);
  const [modalOpen, setModalOpen] = useState(false);
  const [editingSid, setEditingSid] = useState<string | null>(null);
  const [form, setForm] = useState<SystemForm>(EMPTY);
  const [tagInput, setTagInput] = useState("");
  const [urlInput, setUrlInput] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [hint, setHint] = useState<{ sap: string } | null>(null);

  const load = () => fetch(`${API}/api/systems`).then(r => r.json()).then(setSystems).catch(() => {});
  useEffect(() => { load(); }, []);

  const openAdd = () => { setEditingSid(null); setForm(EMPTY); setError(null); setModalOpen(true); };
  const openEdit = (s: SystemRow) => {
    setEditingSid(s.sid);
    setForm(rowToForm(s));
    setError(null);
    setModalOpen(true);
  };

  const save = async () => {
    setError(null);
    const method = editingSid ? "PUT" : "POST";
    const url = editingSid ? `${API}/api/systems/${editingSid}` : `${API}/api/systems`;
    const res = await fetch(url, {
      method, headers: { "Content-Type": "application/json" },
      body: JSON.stringify(formToBody(form)),
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
        <b>{systems.length} systems</b> in the registry (D-006/D-021). SAP
        WebGUI login credentials are never stored here (D-018) — set them as
        environment variables. SSH credentials for app/dialog servers and DB
        hosts ARE stored here, encrypted (D-023).
      </p>

      <div style={{ marginBottom: 16 }}>
        <button className="btn" onClick={openAdd}>+ Add System</button>
      </div>

      <div className="list">
        {systems.length === 0 && (
          <div className="row"><span className="rsub">No systems yet — click &quot;Add System&quot;.</span></div>
        )}
        {systems.map(s => {
          const sshHosts = s.credentials.ssh_hosts;
          const sshMissing = sshHosts.filter(h => !h.credential_set).length;
          return (
            <div className="row" key={s.sid}>
              <div className="grow">
                <div className="rtitle">{s.sid} <span style={{ color: "var(--text-3)", fontWeight: 400 }}>
                  · {s.release_tag} · {s.auth}</span></div>
                <div className="rsub">{s.webgui_url}</div>
                <div style={{ marginTop: 6 }}>
                  <span className={s.credentials.sap_credentials ? "badge-ok" : "badge-missing"}>
                    {s.credentials.sap_credentials ? "● SAP credentials set" : "○ SAP credentials missing"}
                  </span>
                  {sshHosts.length > 0 && (
                    <span className={sshMissing === 0 ? "badge-ok" : "badge-missing"} style={{ marginLeft: 12 }}>
                      {sshMissing === 0 ? `● SSH set for all ${sshHosts.length} host(s)` : `○ SSH missing for ${sshMissing}/${sshHosts.length} host(s)`}
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
          );
        })}
      </div>

      {hint && (
        <div className="panel" style={{ padding: 16, marginTop: 16, fontFamily: "monospace", fontSize: 12 }}>
          <div>{hint.sap}</div>
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
              Core BASIS connection topology (D-021)
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

            <h3 style={{ fontSize: 12.5, color: "var(--text-2)", margin: "16px 0 10px", borderTop: "1px solid var(--hairline)", paddingTop: 14 }}>
              SSH credentials for filesystem checks (D-023 — encrypted at rest)
            </h3>

            <div className="field-group">
              <label className="field-label">Default credential (used for the main host, and any server/DB host below without its own override)</label>
              <CredentialFields cred={form.ssh_credential} onChange={c => setForm({ ...form, ssh_credential: c })} />
            </div>

            <HostList label="Additional app/dialog servers" hosts={form.servers}
                      setHosts={h => setForm({ ...form, servers: h })} />

            <HostList label="DB hosts (single or multiple)" hosts={form.db_hosts}
                      setHosts={h => setForm({ ...form, db_hosts: h })} />

            <div style={{ fontSize: 11, color: "var(--text-3)", margin: "10px 0 16px" }}>
              SAP WebGUI login isn&apos;t set here — after saving, export
              SAP_{form.sid || "<SID>"}_USER / _PASSWORD on the machine
              running the backend.
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
