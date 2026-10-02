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

// ── D-052: Performance ─────────────────────────────────────────────────────
export type Rating = "good" | "warning" | "serious" | "critical" | "unknown";
export interface PerfKpi {
  id: string; label: string; value: number | null; unit: string; rating: Rating;
  detail: string; target: string; group: "SAP" | "HANA"; spark: number[];
}
export interface HourPoint {
  hour: number; label?: string; steps: number; dia_steps: number; avg_resp: number;
  cpu: number; db: number; wait: number; other: number;
}
export interface TaskLine {
  code: string; name: string; steps: number; avg_resp: number; avg_cpu: number; avg_db: number;
  avg_wait: number; avg_rollwait: number; avg_gui: number; avg_load: number; total_resp_s: number;
}
export interface TopTx {
  name: string; task: string; steps: number; total_s: number; share: number;
  avg_resp: number; avg_cpu: number; avg_db: number; users: number | null;
}
export interface Workload { date: string; tasks: TaskLine[]; hourly: HourPoint[]; top: TopTx[]; steps: number }
export interface WpCount { total: number; running: number; waiting: number; hold: number; stopped: number }
export interface SmonPoint {
  time: string; act_wps: number; act_dia: number; cpu: number; free_mem: number; diaq: number;
  updq: number; enqq: number; users: number; sessions: number; locks: number; servers: number;
}
export interface Smon {
  installed: boolean; active: boolean; last: string | null; why?: string;
  series?: SmonPoint[]; latest?: SmonPoint | null;
}
export interface HanaHour { hour: number; cpu_avg: number; cpu_max: number; mem_avg_gb: number; mem_max_gb: number; limit_gb: number }
export interface HanaSql { hash: string; executions: number; total_s: number; avg_ms: number; share: number; statement: string }
export interface HanaLive {
  hosts: { host: string; cpu: number | null; mem_used_gb: number | null; mem_limit_gb: number | null;
           disk_used_gb?: number | null; disk_size_gb?: number | null }[];
  connections: number | null; running_statements: number | null; blocked: number | null;
  alerts: Record<string, number>; unloads_24h: number | null;
  savepoint_avg_s: number | null; savepoint_max_s: number | null; top_sql: HanaSql[]; today?: HanaHour[];
}
export interface SapLive {
  servers: { name: string; host: string; active: boolean; wp: Record<string, WpCount>; busy_dia: number; total_dia: number }[];
  wp: Record<string, WpCount>; dia_util: number | null;
  cpu: { util: number | null; cpus: number | null; load1: number; load15: number };
  memory: { phys_mb: number | null; free_mb: number | null; free_pct: number | null; swap_used_pct: number | null };
  users: number | null; sessions: number | null; locks: number | null;
  updates_pending: number | null; update_errors: number | null; jobs_running: number | null;
  buffers: { buffers: { name: string; hit: number | null; swaps: number; free_pct: number | null; requests: number }[];
             swaps: number; worst_hit: number | null } | null;
}
export interface PerfView {
  mode: "live" | "day"; date?: string; sid: string; client: string; release: string; db: string; host: string;
  as_of: string; overall: { score: number | null; grade: string; rating: Rating; counts: Record<string, number> };
  kpis: PerfKpi[]; findings: { kpi: string; rating: Rating; title: string; detail: string; recommendation: string }[];
  sap?: SapLive; workload: Workload | null; hourly: HourPoint[]; smon: Smon; hana?: HanaLive | null;
  hana_hours?: HanaHour[]; notes: string[]; took_s: number; calls: number;
}
export interface PerfRanges {
  sid: string; today: string; workload_days: string[]; hana_from: string | null; oldest: string | null;
  limit_days: number; smon: Smon; notes: string[];
}

export const perfApi = {
  live: (sid: string) => fetch(`${apiBase()}/api/perf/${encodeURIComponent(sid)}/live`).then(r => asJson<PerfView>(r)),
  day: (sid: string, date: string) =>
    fetch(`${apiBase()}/api/perf/${encodeURIComponent(sid)}/day?date=${date}`).then(r => asJson<PerfView>(r)),
  ranges: (sid: string) => fetch(`${apiBase()}/api/perf/${encodeURIComponent(sid)}/ranges`).then(r => asJson<PerfRanges>(r)),
  scheduleSmon: (sid: string) => fetch(`${apiBase()}/api/perf/${encodeURIComponent(sid)}/smon/schedule`, { method: "POST" })
    .then(r => asJson<{ ok: boolean; message: string }>(r)),
  /** Builds the file from the exact view on screen and saves it. */
  exportView: async (view: PerfView, format: "xlsx" | "pdf") => {
    const r = await fetch(`${apiBase()}/api/perf/export?format=${format}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(view),
    });
    if (!r.ok) await asJson(r);
    // Content-Disposition isn't readable cross-origin (no Expose-Headers), so name it here.
    const when = view.mode === "day" ? view.date : view.as_of.slice(0, 16).replace(" ", "_").replace(":", "");
    const name = `Performance_${view.sid}_${when}.${format}`.replace(/[^A-Za-z0-9._-]/g, "_");
    const url = URL.createObjectURL(await r.blob());
    const a = document.createElement("a");
    a.href = url; a.download = name; document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
  },
};
