"use client";

/* Reports — generated Word/PDF artifacts (D-014's report_composer output,
   tracked in RunStore's report_runs table), with per-report download and
   delete (D-032). */

import { useCallback, useEffect, useState } from "react";
import { apiBase } from "../lib/api";

const API = apiBase();

type ReportRow = {
  run_id: string; sid: string; docx_path: string | null;
  pdf_path: string | null; created_at: string;
};

export default function Reports() {
  const [reports, setReports] = useState<ReportRow[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [confirming, setConfirming] = useState<string | null>(null);

  const load = useCallback(() => {
    fetch(`${API}/api/reports?limit=50`).then(r => r.json()).then(setReports)
      .catch(err => setError(`Could not reach the backend at ${API}: ${err instanceof Error ? err.message : String(err)}`));
  }, []);

  useEffect(load, [load]);

  const remove = async (r: ReportRow) => {
    const key = `${r.run_id}-${r.sid}`;
    setBusy(key);
    setError("");
    try {
      const res = await fetch(`${API}/api/reports/${r.run_id}/${r.sid}`, { method: "DELETE" });
      if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
      setReports(prev => prev.filter(x => `${x.run_id}-${x.sid}` !== key));
    } catch (err) {
      setError(`Could not delete this report: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setBusy(null);
      setConfirming(null);
    }
  };

  return (
    <div style={{ maxWidth: 900 }} className="page-enter">
      <h1 className="ltitle">Reports</h1>
      <p className="lsub"><b>{reports.length} generated reports</b> · Word + PDF, A3 landscape</p>

      {error && (
        <div style={{ margin: "0 0 16px", padding: "10px 14px", borderRadius: 10,
                       border: "1px solid var(--red)", color: "var(--red)", fontSize: 12.5 }}>
          {error}
        </div>
      )}

      <div className="list">
        {reports.length === 0 && (
          <div className="row">
            <span className="rsub">
              No reports yet — a report is generated automatically at the end of every run.
            </span>
          </div>
        )}
        {reports.map((r, i) => {
          const key = `${r.run_id}-${r.sid}`;
          return (
            <div className="row rise" key={key} style={{ ["--i" as string]: Math.min(i, 10) }}>
              <span className="rdot" style={{ background: "var(--blue)" }} />
              <div className="grow">
                <div className="rtitle">{r.sid} · run {r.run_id}</div>
                <div className="rsub">{r.created_at}</div>
              </div>
              <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                {r.docx_path && (
                  <a className="btn secondary" href={`${API}/api/reports/${r.run_id}/${r.sid}/download/docx`}
                     style={{ textDecoration: "none" }}>Word</a>
                )}
                {r.pdf_path && (
                  <a className="btn secondary" href={`${API}/api/reports/${r.run_id}/${r.sid}/download/pdf`}
                     style={{ textDecoration: "none" }}>PDF</a>
                )}
                <a className="btn secondary" href={`${API}/api/reports/${r.run_id}/download/zip`}
                   title="Every report from this run, zipped" style={{ textDecoration: "none" }}>ZIP</a>

                {confirming === key ? (
                  <>
                    <button className="btn danger" disabled={busy === key} onClick={() => remove(r)}>
                      {busy === key ? "Deleting…" : "Confirm"}
                    </button>
                    <button className="btn secondary" onClick={() => setConfirming(null)}>Cancel</button>
                  </>
                ) : (
                  <button className="btn secondary" title="Delete this report (Word + PDF files too)"
                          onClick={() => setConfirming(key)}
                          style={{ color: "var(--red)" }}>Delete</button>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
