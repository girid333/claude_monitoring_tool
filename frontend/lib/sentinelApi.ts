import { apiBase as appApiBase } from "@/app/lib/api";

// Backend URL resolved at RUNTIME (D-024). Delegates to the app's existing
// helper (app/lib/api.ts) so Sentinel pages and the rest of the app always
// agree on the backend origin; NEXT_PUBLIC_API_BASE still overrides it.
export function apiBase(): string {
  const env = process.env.NEXT_PUBLIC_API_BASE;
  if (env) return env.replace(/\/$/, "");
  return appApiBase().replace(/\/$/, "");
}

export type Triage = "ok" | "expected" | "review" | "act" | "no_data";
export type Tone = "neutral" | "good" | "warn" | "bad";

export interface Metric { label: string; value: string; tone: Tone }
export interface BarItem { label: string; value: number; tone: Tone; note: string }
export interface Chart { title: string; unit: string; items: BarItem[] }
export interface TableOut { title: string; columns: string[]; rows: string[][]; total: number }
export interface Check {
  id: string; category: string; task: string; tcode: string; description: string;
  period: string; sap_status: "ok" | "not_ok" | "unknown"; sap_evidence: string;
  triage: Triage; headline: string; findings: string[]; recommendation: string | null;
  metrics: Metric[]; charts: Chart[]; sheet: string | null; tables: TableOut[];
  text: string | null; analysis_note: string | null;
}
export interface Summary {
  total: number; sap_ok: number; sap_not_ok: number; ok: number;
  expected: number; review: number; act: number; no_data: number;
}
export interface HealthReport {
  id: string; filename: string; imported_at: string; report_date: string | null;
  title: string; system: { sid: string; client: string; instances: string[] };
  summary: Summary; checks: Check[]; extra_sheets: string[];
  source?: "import" | "sap_pull"; has_workbook?: boolean;
}
export interface ReportListItem {
  id: string; filename: string; imported_at: string; report_date: string | null;
  sid: string; client: string; summary: Summary; source?: "import" | "sap_pull";
}
export interface SystemItem { sid: string; system_name?: string }

async function asJson<T>(r: Response): Promise<T> {
  if (!r.ok) {
    let msg = `Request failed (${r.status}).`;
    try { const b = await r.json(); if (b?.detail) msg = String(b.detail); } catch { /* keep default */ }
    throw new Error(msg);
  }
  return r.json() as Promise<T>;
}

export const healthApi = {
  list: () => fetch(`${apiBase()}/api/health/reports`).then(r => asJson<ReportListItem[]>(r)),
  get: (id: string) => fetch(`${apiBase()}/api/health/reports/${id}`).then(r => asJson<HealthReport>(r)),
  latest: () => fetch(`${apiBase()}/api/health/reports/latest`).then(r => asJson<HealthReport | null>(r)),
  upload: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return fetch(`${apiBase()}/api/health/reports`, { method: "POST", body: fd })
      .then(r => asJson<HealthReport>(r));
  },
  // D-051: collect the health check straight from SAP over SOAP RFC.
  pull: (sid: string) => fetch(`${apiBase()}/api/health/pull`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ sid }),
  }).then(r => asJson<HealthReport>(r)),
  systems: () => fetch(`${apiBase()}/api/systems`).then(r => asJson<SystemItem[]>(r)),
  workbookUrl: (id: string) => `${apiBase()}/api/health/reports/${id}/workbook`,
  remove: (id: string) => fetch(`${apiBase()}/api/health/reports/${id}`, { method: "DELETE" })
    .then(r => { if (!r.ok && r.status !== 404) throw new Error(`Delete failed (${r.status}).`); }),
};

export const TRIAGE_LABEL: Record<Triage, string> = {
  act: "Needs action", review: "Review", expected: "Expected behaviour",
  ok: "Passed", no_data: "No evidence",
};

export function clearCount(s: Summary): number { return s.ok + s.expected; }
