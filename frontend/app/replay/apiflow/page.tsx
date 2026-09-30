"use client";

/* D-048: API Flow composer.

   The replacement for screen recording. You pick real operations that were
   DISCOVERED from the system (OData entity sets and function imports from
   $metadata, RFC-enabled function modules), wire them into an ordered
   flow, bind one step's output to the next step's input, and replay it
   headlessly.

   The design rule throughout: never make the operator type a name the
   system could have offered. Every service, entity set and field comes
   from a dropdown populated by live metadata — which is also why the
   "Resource not found for URI segment" class of error largely disappears
   here. */

import { useEffect, useState } from "react";
import { apiBase } from "../../lib/api";

const API = apiBase();

type SystemRow = { sid: string; system_name?: string };
type ProbedService = {
  service: string; title: string; url: string;
  state: string; detail: string; operations: number;
};
type ODataProperty = { name: string; type: string; nullable: boolean; is_key: boolean };
type Operation = {
  kind: string; name: string; http_method: string;
  keys: string[]; properties: ODataProperty[]; parameters: ODataProperty[];
};
type Step = {
  id: string; kind: string; description: string;
  service: string; entity_set: string; function_import: string;
  filter: string; top: number; function: string;
  params: Record<string, string>; extract: Record<string, string>;
};
type StepResult = {
  id: string; kind: string; ok: boolean; detail: string; error: string;
  row_count: number; rows: Record<string, unknown>[];
  extracted: Record<string, unknown>; skipped: boolean; duration_ms: number;
};
type CheckResult = { description: string; ok: boolean; detail: string };
type RunResult = {
  flow_id: string; ok: boolean; error: string; duration_ms: number;
  steps: StepResult[]; checks: CheckResult[];
};
type SavedFlow = {
  flow_id: string; description: string; step_count: number; check_count: number;
};

const blankStep = (n: number): Step => ({
  id: `s${n}`, kind: "odata_read", description: "",
  service: "", entity_set: "", function_import: "", filter: "", top: 20,
  function: "", params: {}, extract: {},
});

export default function ApiFlowPage() {
  const [systems, setSystems] = useState<SystemRow[]>([]);
  const [sid, setSid] = useState("");
  const [services, setServices] = useState<ProbedService[]>([]);
  const [probing, setProbing] = useState(false);
  const [serviceFilter, setServiceFilter] = useState("");
  const [metaCache, setMetaCache] = useState<Record<string, Operation[]>>({});
  const [steps, setSteps] = useState<Step[]>([blankStep(1)]);
  const [flowId, setFlowId] = useState("my_flow");
  const [description, setDescription] = useState("");
  const [result, setResult] = useState<RunResult | null>(null);
  const [saved, setSaved] = useState<SavedFlow[]>([]);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const loadSaved = () => {
    fetch(`${API}/api/rfc/flows`).then(r => r.json()).then(setSaved).catch(() => {});
  };

  useEffect(() => {
    fetch(`${API}/api/systems`).then(r => r.json()).then((s: SystemRow[]) => {
      setSystems(s);
      if (s.length) setSid(prev => prev || s[0].sid);
    }).catch(err => setError(`Could not reach the backend at ${API}: ${err}`));
    loadSaved();
  }, []);

  const probe = async () => {
    setProbing(true); setError(""); setNotice("");
    try {
      const r = await fetch(`${API}/api/rfc/odata/probe`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sid, only_usable: true }),
      });
      const j = await r.json();
      if (!j.ok) throw new Error(j.note || "probe failed");
      setServices(j.services || []);
      setNotice(j.note || "");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally { setProbing(false); }
  };

  const loadMetadata = async (service: string) => {
    if (metaCache[service]) return metaCache[service];
    const r = await fetch(`${API}/api/rfc/odata/metadata`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sid, service }),
    });
    const j = await r.json();
    const ops: Operation[] = j.ok ? j.operations : [];
    setMetaCache(prev => ({ ...prev, [service]: ops }));
    return ops;
  };

  const update = (i: number, patch: Partial<Step>) =>
    setSteps(prev => prev.map((s, k) => (k === i ? { ...s, ...patch } : s)));

  const pickService = async (i: number, url: string) => {
    update(i, { service: url, entity_set: "", function_import: "" });
    if (url) await loadMetadata(url);
  };

  const buildFlow = () => ({
    flow_id: flowId, description,
    applies_to: sid ? [sid] : [],
    params: {},
    steps: steps.map(s => {
      const base: Record<string, unknown> = {
        id: s.id, kind: s.kind, description: s.description,
        extract: s.extract, params: s.params,
      };
      if (s.kind === "odata_read")
        return { ...base, service: s.service, entity_set: s.entity_set,
                 filter: s.filter, top: s.top };
      if (s.kind === "odata_function")
        return { ...base, service: s.service, function_import: s.function_import };
      return { ...base, function: s.function };
    }),
    checks: [],
  });

  const run = async () => {
    setBusy("run"); setError(""); setResult(null);
    try {
      const r = await fetch(`${API}/api/rfc/flows/run`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sid, flow: buildFlow() }),
      });
      const j = await r.json();
      if (j.detail) throw new Error(j.detail);
      setResult(j);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally { setBusy(""); }
  };

  const save = async () => {
    setBusy("save"); setError(""); setNotice("");
    try {
      const r = await fetch(`${API}/api/rfc/flows/save`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ flow: buildFlow() }),
      });
      const j = await r.json();
      if (!j.ok) {
        // Validation problems are listed in full, before anything is saved.
        setError("Flow is not valid: " + (j.problems || []).join("; "));
        return;
      }
      setNotice(`Saved ${j.flow_id} (${j.step_count} steps)`);
      loadSaved();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally { setBusy(""); }
  };

  const loadFlow = async (id: string) => {
    const r = await fetch(`${API}/api/rfc/flows/${id}`);
    const j = await r.json();
    setFlowId(j.flow_id);
    setDescription(j.description || "");
    setSteps((j.steps || []).map((s: Record<string, unknown>) => ({
      ...blankStep(1), ...s,
      params: (s.params as Record<string, string>) || {},
      extract: (s.extract as Record<string, string>) || {},
    })));
    setResult(null);
    for (const s of j.steps || []) if (s.service) await loadMetadata(s.service);
  };

  const removeFlow = async (id: string) => {
    await fetch(`${API}/api/rfc/flows/${id}`, { method: "DELETE" }).catch(() => {});
    loadSaved();
  };

  const shown = services.filter(s =>
    !serviceFilter || (s.service + s.title).toLowerCase().includes(serviceFilter.toLowerCase()));

  return (
    <div style={{ maxWidth: 1250 }} className="page-enter">
      <h1 className="ltitle">API Flows</h1>
      <p className="lsub">
        Build a flow from operations the system actually exposes — OData entity sets and
        function imports read from <code>$metadata</code>, and RFC-enabled function
        modules — then replay it headlessly. One step&apos;s output can feed the next via{" "}
        <code>{"{{step.name}}"}</code>, so this does what a recording does without
        depending on screens.
      </p>

      {error && (
        <div style={{ margin: "0 0 16px", padding: "10px 14px", borderRadius: 10,
                       border: "1px solid var(--red)", color: "var(--red)", fontSize: 12.5 }}>
          {error}
        </div>
      )}
      {notice && (
        <div style={{ margin: "0 0 16px", padding: "10px 14px", borderRadius: 10,
                       border: "1px solid var(--green)", color: "var(--green)", fontSize: 12.5 }}>
          {notice}
        </div>
      )}

      {/* ── system + service catalogue ─────────────────────────────── */}
      <div className="panel" style={{ padding: "14px 18px", marginBottom: 18, borderRadius: 14 }}>
        <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
          <label style={{ fontSize: 12.5, color: "var(--text-2)" }}>System</label>
          <select className="input" value={sid} onChange={e => setSid(e.target.value)}
                  style={{ minWidth: 200 }}>
            {systems.length === 0 && <option value="">No systems — add one first</option>}
            {systems.map(s => (
              <option key={s.sid} value={s.sid}>
                {s.system_name ? `${s.system_name} (${s.sid})` : s.sid}
              </option>
            ))}
          </select>
          <button className="btn secondary" disabled={!sid || probing} onClick={probe}>
            {probing ? "Probing services…" : "Load available services"}
          </button>
          {services.length > 0 && (
            <input className="input" value={serviceFilter}
                   onChange={e => setServiceFilter(e.target.value)}
                   placeholder="filter services…" style={{ width: 200 }} />
          )}
          <span style={{ fontSize: 11.5, color: "var(--text-3)" }}>
            {services.length > 0
              ? `${shown.length} of ${services.length} callable service(s)`
              : "Probe first — only services that actually respond are offered."}
          </span>
        </div>
      </div>

      {/* ── steps ──────────────────────────────────────────────────── */}
      {steps.map((step, i) => {
        const ops = metaCache[step.service] || [];
        const entitySets = ops.filter(o => o.kind === "entityset");
        const functions = ops.filter(o => o.kind === "function");
        const chosen = ops.find(o =>
          o.name === (step.kind === "odata_read" ? step.entity_set : step.function_import));
        return (
          <div key={i} className="panel"
               style={{ padding: "14px 18px", marginBottom: 12, borderRadius: 14 }}>
            <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
              <span style={{ fontSize: 12, color: "var(--text-3)" }}>Step {i + 1}</span>
              <input className="input" value={step.id}
                     onChange={e => update(i, { id: e.target.value })}
                     style={{ width: 70 }} title="Step id — used in {{id.name}} references" />
              <select className="input" value={step.kind}
                      onChange={e => update(i, { kind: e.target.value })}>
                <option value="odata_read">OData: read entity set</option>
                <option value="odata_function">OData: call function import</option>
                <option value="rfc_call">RFC: call function module</option>
              </select>
              <input className="input" value={step.description}
                     onChange={e => update(i, { description: e.target.value })}
                     placeholder="what this step does" style={{ flex: 1, minWidth: 160 }} />
              <button className="btn secondary" style={{ fontSize: 11.5, padding: "3px 10px" }}
                      onClick={() => setSteps(prev => prev.filter((_, k) => k !== i))}
                      disabled={steps.length === 1}>Remove</button>
            </div>

            {step.kind === "rfc_call" ? (
              <div style={{ marginTop: 10, display: "flex", gap: 10, flexWrap: "wrap" }}>
                <input className="input" value={step.function}
                       onChange={e => update(i, { function: e.target.value.toUpperCase() })}
                       placeholder="RFC_SYSTEM_INFO" style={{ width: 260 }} />
              </div>
            ) : (
              <div style={{ marginTop: 10, display: "flex", gap: 10, flexWrap: "wrap" }}>
                <select className="input" value={step.service}
                        onChange={e => pickService(i, e.target.value)}
                        style={{ minWidth: 280 }}>
                  <option value="">Select a service…</option>
                  {shown.map(s => (
                    <option key={s.service} value={s.url}>
                      {s.service} ({s.operations} ops)
                    </option>
                  ))}
                </select>
                {step.kind === "odata_read" ? (
                  <select className="input" value={step.entity_set}
                          onChange={e => update(i, { entity_set: e.target.value })}
                          style={{ minWidth: 220 }}>
                    <option value="">
                      {step.service ? "Select an entity set…" : "pick a service first"}
                    </option>
                    {entitySets.map(o => <option key={o.name} value={o.name}>{o.name}</option>)}
                  </select>
                ) : (
                  <select className="input" value={step.function_import}
                          onChange={e => update(i, { function_import: e.target.value })}
                          style={{ minWidth: 220 }}>
                    <option value="">
                      {step.service ? "Select a function import…" : "pick a service first"}
                    </option>
                    {functions.map(o => (
                      <option key={o.name} value={o.name}>{o.name} ({o.http_method})</option>
                    ))}
                  </select>
                )}
                {step.kind === "odata_read" && (
                  <>
                    <input className="input" value={step.filter}
                           onChange={e => update(i, { filter: e.target.value })}
                           placeholder="$filter e.g. Severity gt 2"
                           style={{ flex: 1, minWidth: 180 }} />
                    <input className="input" type="number" value={step.top}
                           onChange={e => update(i, { top: Number(e.target.value) })}
                           style={{ width: 80 }} title="$top" />
                  </>
                )}
              </div>
            )}

            {/* fields the chosen operation really has — no guessing */}
            {chosen && (
              <div style={{ marginTop: 8, fontSize: 11, color: "var(--text-3)" }}>
                {chosen.kind === "entityset" ? (
                  <>Fields: {chosen.properties.slice(0, 12).map(p => (
                    <span key={p.name} style={{ marginRight: 8 }}>
                      <code style={{ color: p.is_key ? "var(--blue)" : undefined }}>
                        {p.name}
                      </code>
                      <span style={{ opacity: .6 }}>:{p.type.replace("Edm.", "")}</span>
                    </span>
                  ))}{chosen.properties.length > 12 && "…"}</>
                ) : (
                  <>Parameters: {chosen.parameters.map(p => (
                    <span key={p.name} style={{ marginRight: 8 }}>
                      <code>{p.name}</code>
                      <span style={{ opacity: .6 }}>:{p.type.replace("Edm.", "")}</span>
                    </span>
                  )) || "none"}</>
                )}
              </div>
            )}

            {/* extract + params: the wiring between steps */}
            <div style={{ marginTop: 10, display: "flex", gap: 10, flexWrap: "wrap" }}>
              <input className="input" style={{ flex: 1, minWidth: 220 }}
                     value={Object.entries(step.extract).map(([k, v]) => `${k}=${v}`).join(", ")}
                     onChange={e => update(i, {
                       extract: Object.fromEntries(e.target.value.split(",")
                         .map(p => p.split("=").map(x => x.trim()))
                         .filter(p => p.length === 2 && p[0]) as [string, string][]),
                     })}
                     placeholder="extract e.g. first=rows[0].AlertGuid"
                     title="Name values from this step's result so later steps can use them" />
              <input className="input" style={{ flex: 1, minWidth: 220 }}
                     value={Object.entries(step.params).map(([k, v]) => `${k}=${v}`).join(", ")}
                     onChange={e => update(i, {
                       params: Object.fromEntries(e.target.value.split(",")
                         .map(p => p.split("=").map(x => x.trim()))
                         .filter(p => p.length === 2 && p[0]) as [string, string][]),
                     })}
                     placeholder={`params e.g. AlertGuid={{${steps[0]?.id || "s1"}.first}}`}
                     title="Parameters for this step; reference earlier steps with {{step.name}}" />
            </div>
          </div>
        );
      })}

      <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap",
                     marginBottom: 18 }}>
        <button className="btn secondary"
                onClick={() => setSteps(prev => [...prev, blankStep(prev.length + 1)])}>
          + Add step
        </button>
        <input className="input" value={flowId} onChange={e => setFlowId(e.target.value)}
               placeholder="flow id" style={{ width: 160 }} />
        <input className="input" value={description}
               onChange={e => setDescription(e.target.value)}
               placeholder="description" style={{ width: 240 }} />
        <button className="btn" disabled={!sid || !!busy} onClick={run}>
          {busy === "run" ? "Running…" : "▶ Run"}
        </button>
        <button className="btn secondary" disabled={!!busy || !flowId.trim()} onClick={save}>
          {busy === "save" ? "Saving…" : "Save flow"}
        </button>
      </div>

      {/* ── result ─────────────────────────────────────────────────── */}
      {result && (
        <div className="panel" style={{
          padding: "14px 18px", marginBottom: 18, borderRadius: 14,
          borderLeft: `3px solid ${result.ok ? "var(--green)" : "var(--red)"}`,
        }}>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8,
                         color: result.ok ? "var(--green)" : "var(--red)" }}>
            {result.ok ? "✓ Flow succeeded" : "✗ Flow failed"}
            <span style={{ color: "var(--text-3)", fontWeight: 400, marginLeft: 8 }}>
              {result.duration_ms} ms
            </span>
          </div>
          {result.error && (
            <div style={{ fontSize: 12, color: "var(--red)", marginBottom: 8 }}>{result.error}</div>
          )}
          {result.steps.map(s => (
            <div key={s.id} style={{ borderTop: "1px solid var(--hairline)", padding: "8px 0" }}>
              <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                <span style={{ fontSize: 12, fontWeight: 600 }}>
                  {s.skipped ? "⤼" : s.ok ? "✓" : "✗"} {s.id}
                </span>
                <span style={{ fontSize: 11, color: "var(--text-3)" }}>{s.kind}</span>
                <span style={{ fontSize: 11.5, color: "var(--text-2)", marginLeft: "auto" }}>
                  {s.detail || (s.skipped ? "skipped" : "")}
                </span>
              </div>
              {s.error && (
                <div style={{ marginTop: 6, padding: "6px 9px", borderRadius: 7, fontSize: 11.5,
                               background: "rgba(255,69,58,.10)", color: "var(--red)" }}>
                  {s.error}
                </div>
              )}
              {Object.keys(s.extracted || {}).length > 0 && (
                <div style={{ marginTop: 6, fontSize: 11, color: "var(--text-3)" }}>
                  extracted: {Object.entries(s.extracted)
                    .map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(", ")}
                </div>
              )}
              {s.rows && s.rows.length > 0 && (
                <div style={{ marginTop: 8, overflowX: "auto" }}>
                  <table style={{ width: "100%", fontSize: 11, borderCollapse: "collapse" }}>
                    <thead>
                      <tr style={{ color: "var(--text-3)", textAlign: "left" }}>
                        {Object.keys(s.rows[0]).slice(0, 10).map(k => (
                          <th key={k} style={{ padding: "3px 8px", whiteSpace: "nowrap" }}>{k}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {s.rows.slice(0, 15).map((row, ri) => (
                        <tr key={ri} style={{ borderTop: "1px solid var(--hairline)" }}>
                          {Object.keys(s.rows[0]).slice(0, 10).map(k => (
                            <td key={k} style={{ padding: "3px 8px", whiteSpace: "nowrap" }}>
                              {String(row[k] ?? "")}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          ))}
          {result.checks.length > 0 && (
            <div style={{ marginTop: 10, fontSize: 12 }}>
              {result.checks.map((c, ci) => (
                <div key={ci} style={{ color: c.ok ? "var(--green)" : "var(--red)" }}>
                  {c.ok ? "✓" : "✗"} {c.description} — {c.detail}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* ── saved flows ────────────────────────────────────────────── */}
      <div className="panel" style={{ padding: "14px 18px", borderRadius: 14 }}>
        <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>
          Saved flows ({saved.length})
        </div>
        {saved.length === 0 && <div className="rsub">Nothing saved yet.</div>}
        {saved.map(f => (
          <div key={f.flow_id} style={{ display: "flex", gap: 10, alignItems: "center",
                                          borderTop: "1px solid var(--hairline)", padding: "8px 0" }}>
            <span style={{ fontSize: 12.5, fontWeight: 600 }}>{f.flow_id}</span>
            <span style={{ fontSize: 11.5, color: "var(--text-3)" }}>
              {f.step_count} step(s){f.description ? ` — ${f.description}` : ""}
            </span>
            <div style={{ marginLeft: "auto", display: "flex", gap: 8 }}>
              <button className="btn secondary" style={{ fontSize: 11.5, padding: "3px 10px" }}
                      onClick={() => loadFlow(f.flow_id)}>Load</button>
              <button className="btn secondary" style={{ fontSize: 11.5, padding: "3px 10px" }}
                      onClick={() => removeFlow(f.flow_id)}>Delete</button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
