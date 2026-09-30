"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  healthApi, TRIAGE_LABEL,
  type Check, type HealthReport, type ReportListItem, type Triage,
} from "@/lib/sentinelApi";
import CheckDrawer from "./CheckDrawer";
import Rings, { RingsLegend } from "./Rings";

type Filter = "all" | Triage;
const FILTERS: { key: Filter; label: string }[] = [
  { key: "all", label: "All" }, { key: "act", label: "Needs action" },
  { key: "review", label: "Review" }, { key: "expected", label: "Expected" }, { key: "ok", label: "Passed" },
];
const CATEGORY_ORDER = ["Security", "Database", "Availability", "Logs and dumps",
  "Jobs and output", "Performance", "Users and sessions", "Other"];

function fmtDate(iso: string | null): string {
  if (!iso) return "Unknown date";
  const d = new Date(iso.length === 10 ? `${iso}T00:00:00` : iso);
  return isNaN(d.getTime()) ? iso : d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

function joinCounts(parts: [number, string, string][]): string {
  const xs = parts.filter(([n]) => n > 0).map(([n, one, many]) => `${n} ${n === 1 ? one : many}`);
  if (!xs.length) return "";
  return xs.length === 1 ? xs[0] : `${xs.slice(0, -1).join(", ")} and ${xs[xs.length - 1]}`;
}

export function verdictSentence(r: HealthReport): string {
  const s = r.summary;
  const tail = joinCounts([
    [s.act, "needs action", "need action"],
    [s.review, "is worth a review", "are worth a review"],
    [s.expected, "is expected behaviour", "are expected behaviour"],
  ]);
  if (s.sap_not_ok === 0 && s.act === 0) return `SAP passed all ${s.total} checks, and the evidence agrees.`;
  const head = s.sap_not_ok
    ? `SAP flagged ${s.sap_not_ok} of ${s.total} checks.`
    : `SAP passed all ${s.total} checks.`;
  return tail ? `${head} After reading the evidence: ${tail}.` : head;
}

export default function HealthDashboard() {
  const [reports, setReports] = useState<ReportListItem[] | null>(null);
  const [report, setReport] = useState<HealthReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>("Loading health checks…");
  const [filter, setFilter] = useState<Filter>("all");
  const [open, setOpen] = useState<Check | null>(null);
  const [over, setOver] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const show = useCallback(async (id: string) => {
    setBusy("Loading report…"); setError(null);
    try {
      const r = await healthApi.get(id);
      setReport(r); setFilter("all");
      const u = new URL(window.location.href); u.searchParams.set("id", id);
      window.history.replaceState(null, "", u.toString());
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(null); }
  }, []);

  useEffect(() => {
    (async () => {
      try {
        const list = await healthApi.list();
        setReports(list);
        const wanted = new URLSearchParams(window.location.search).get("id");
        const pick = list.find(x => x.id === wanted)?.id ?? list[0]?.id;
        if (pick) await show(pick); else setBusy(null);
      } catch {
        setError("Cannot reach the Sentinel backend. Check that it is running on port 8000.");
        setBusy(null); setReports([]);
      }
    })();
  }, [show]);

  const upload = async (file: File | undefined | null) => {
    if (!file) return;
    setBusy(`Reading ${file.name}…`); setError(null);
    try {
      const r = await healthApi.upload(file);
      setReports(await healthApi.list());
      setReport(r); setFilter("all");
      const u = new URL(window.location.href); u.searchParams.set("id", r.id);
      window.history.replaceState(null, "", u.toString());
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(null); if (fileRef.current) fileRef.current.value = ""; }
  };

  const remove = async () => {
    if (!report || !window.confirm(`Remove the import of ${report.filename}? The workbook itself is not affected.`)) return;
    try {
      await healthApi.remove(report.id);
      const list = await healthApi.list();
      setReports(list); setReport(null);
      const u = new URL(window.location.href); u.searchParams.delete("id");
      window.history.replaceState(null, "", u.toString());
      if (list[0]) await show(list[0].id);
    } catch (e) { setError((e as Error).message); }
  };

  const byTriage = useMemo(() => {
    const g: Record<Triage, Check[]> = { act: [], review: [], expected: [], ok: [], no_data: [] };
    report?.checks.forEach(c => g[c.triage].push(c));
    return g;
  }, [report]);

  const grouped = useMemo(() => {
    const vis = (report?.checks ?? []).filter(c => filter === "all" || c.triage === filter);
    const m = new Map<string, Check[]>();
    vis.forEach(c => { const k = CATEGORY_ORDER.includes(c.category) ? c.category : "Other"; m.set(k, [...(m.get(k) ?? []), c]); });
    return CATEGORY_ORDER.filter(k => m.has(k)).map(k => [k, m.get(k)!] as const);
  }, [report, filter]);

  const input = (
    <input ref={fileRef} type="file" accept=".xls,.xml,.xlsx" hidden data-testid="sx-file"
      onChange={e => upload(e.target.files?.[0])} />
  );

  return (
    <main className="sx-wrap">
      {input}
      <div className="sx-page-head">
        <div>
          <h1 className="sx-h2">Health check</h1>
          <p className="sx-sub">Import the workbook from SAP&apos;s Automated Health Check. Sentinel reads every tab and triages what SAP flagged.</p>
        </div>
        <div className="sx-tools">
          {reports && reports.length > 0 ? (
            <select className="sx-select" aria-label="Imported health checks" value={report?.id ?? ""}
              onChange={e => e.target.value && show(e.target.value)}>
              {reports.map(r => (
                <option key={r.id} value={r.id}>
                  {r.sid || "Unknown system"} {r.client} — {fmtDate(r.report_date)} ({r.summary.act} to act on)
                </option>
              ))}
            </select>
          ) : null}
          <button className="sx-btn sx-btn-primary" onClick={() => fileRef.current?.click()} disabled={!!busy && busy.startsWith("Reading")}>
            Import workbook
          </button>
        </div>
      </div>

      {error ? <p className="sx-error" role="alert">{error}</p> : null}
      {busy ? <p className="sx-busy" aria-live="polite">{busy}</p> : null}

      {!busy && !report ? (
        <div className="sx-drop" data-over={over}
          onDragOver={e => { e.preventDefault(); setOver(true); }}
          onDragLeave={() => setOver(false)}
          onDrop={e => { e.preventDefault(); setOver(false); upload(e.dataTransfer.files?.[0]); }}>
          <h3>Drop a health check workbook here</h3>
          <p>Use the .XLS file that SAP&apos;s Automated Health Check produces (an XML spreadsheet with a Monitors tab and one tab per transaction). An .xlsx copy works too.</p>
          <button className="sx-btn sx-btn-primary" onClick={() => fileRef.current?.click()}>Choose file</button>
        </div>
      ) : null}

      {report && !busy ? (
        <>
          <section className="sx-panel sx-overview" aria-label="Summary">
            <Rings summary={report.summary} size={220} />
            <div>
              <p className="sx-verdict" data-testid="sx-verdict">{verdictSentence(report)}</p>
              <dl className="sx-facts">
                <div><dt>System</dt><dd>{report.system.sid || "Unknown"}</dd></div>
                <div><dt>Client</dt><dd>{report.system.client || "—"}</dd></div>
                <div><dt>Checked on</dt><dd>{fmtDate(report.report_date)}</dd></div>
                <div><dt>Instances</dt><dd>{report.system.instances.length || "—"}</dd></div>
              </dl>
              <div style={{ marginTop: 18 }}><RingsLegend summary={report.summary} /></div>
            </div>
          </section>

          {(["act", "review", "expected"] as const).map(t => byTriage[t].length ? (
            <section key={t} aria-labelledby={`sx-sec-${t}`}>
              <div className="sx-section">
                <h3 id={`sx-sec-${t}`}>{t === "act" ? "Needs action" : t === "review" ? "Worth a review" : "Flagged by SAP, expected behaviour"}</h3>
                <span className="sx-count">{byTriage[t].length}</span>
              </div>
              <div className="sx-findings">
                {byTriage[t].map(c => (
                  <article key={c.id} className="sx-panel sx-finding" data-testid={`sx-finding-${c.id}`}>
                    <span className={`sx-bar sx-t-${c.triage}`} />
                    <div>
                      <h4><b style={{ color: "var(--text)", marginRight: 8 }}>{c.tcode}</b>{c.task}</h4>
                      <p className="sx-headline">{c.headline}</p>
                      {t !== "expected" && c.findings.length ? (
                        <ul>{c.findings.slice(0, 3).map((f, i) => <li key={i}>{f}</li>)}
                          {c.findings.length > 3 ? <li>{c.findings.length - 3} more in the details</li> : null}</ul>
                      ) : null}
                      {t !== "expected" && c.recommendation ? <p className="sx-rec">{c.recommendation}</p> : null}
                    </div>
                    <button className="sx-link" onClick={() => setOpen(c)}>View evidence</button>
                  </article>
                ))}
              </div>
            </section>
          ) : null)}

          <section aria-labelledby="sx-all">
            <div className="sx-section" style={{ justifyContent: "space-between", flexWrap: "wrap" }}>
              <div style={{ display: "flex", gap: 12, alignItems: "baseline" }}>
                <h3 id="sx-all">All checks</h3><span className="sx-count">{report.summary.total}</span>
              </div>
              <div className="sx-filter" role="group" aria-label="Filter checks">
                {FILTERS.map(f => (
                  <button key={f.key} aria-pressed={filter === f.key} onClick={() => setFilter(f.key)}>{f.label}</button>
                ))}
              </div>
            </div>
            {grouped.length === 0 ? <p className="sx-sub">No checks in this group.</p> : null}
            {grouped.map(([cat, list]) => (
              <div key={cat}>
                <div className="sx-cat">{cat}</div>
                <div className="sx-grid">
                  {list.map(c => (
                    <button key={c.id} className="sx-tile" onClick={() => setOpen(c)} data-testid={`sx-tile-${c.id}`}>
                      <span className="sx-tile-top"><span className={`sx-dot sx-t-${c.triage}`} aria-hidden />
                        <b>{c.tcode}</b><span style={{ marginLeft: "auto" }} className={`sx-tag sx-t-${c.triage}`}>{TRIAGE_LABEL[c.triage]}</span></span>
                      <span style={{ fontSize: 13.5 }}>{c.task}</span>
                      <p>{c.headline}</p>
                    </button>
                  ))}
                </div>
              </div>
            ))}
          </section>

          <p className="sx-foot" style={{ marginTop: 40 }}>
            Imported from {report.filename}. Sentinel&apos;s assessment is a reading of SAP&apos;s evidence, not a replacement for it; every check links to the underlying tab.{" "}
            <button className="sx-link" onClick={remove}>Remove this import</button>
          </p>
        </>
      ) : null}

      {open ? <CheckDrawer check={open} onClose={() => setOpen(null)} /> : null}
    </main>
  );
}
