"use client";

/* Reports — lists generated Word/PDF artifacts (D-014's report_composer
   output, tracked in RunStore's report_runs table) with direct download
   links to the /api/reports/{run_id}/{sid}/download/{kind} route. */

import { useEffect, useState } from "react";

const API = process.env.NEXT_PUBLIC_API ?? "http://localhost:8000";

type ReportRow = {
  run_id: string; sid: string; docx_path: string | null;
  pdf_path: string | null; created_at: string;
};

export default function Reports() {
  const [reports, setReports] = useState<ReportRow[]>([]);

  useEffect(() => {
    fetch(`${API}/api/reports?limit=50`).then(r => r.json()).then(setReports).catch(() => {});
  }, []);

  return (
    <div style={{ maxWidth: 900 }}>
      <h1 className="ltitle">Reports</h1>
      <p className="lsub"><b>{reports.length} generated reports</b> · Word + PDF, A3 landscape (D-014)</p>

      <div className="list">
        {reports.length === 0 && (
          <div className="row">
            <span className="rsub">
              No reports yet — a report is generated automatically whenever a run
              includes flows or Core BASIS.
            </span>
          </div>
        )}
        {reports.map((r, i) => (
          <div className="row" key={`${r.run_id}-${r.sid}-${i}`}>
            <span className="rdot" style={{ background: "var(--blue)" }} />
            <div className="grow">
              <div className="rtitle">{r.sid} · run {r.run_id}</div>
              <div className="rsub">{r.created_at}</div>
            </div>
            <div style={{ display: "flex", gap: 8 }}>
              {r.docx_path && (
                <a className="btn secondary"
                   href={`${API}/api/reports/${r.run_id}/${r.sid}/download/docx`}
                   style={{ textDecoration: "none" }}>Word</a>
              )}
              {r.pdf_path && (
                <a className="btn secondary"
                   href={`${API}/api/reports/${r.run_id}/${r.sid}/download/pdf`}
                   style={{ textDecoration: "none" }}>PDF</a>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
