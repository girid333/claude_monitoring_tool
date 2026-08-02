"use client";

/* D-031: global run state.

   Before this, run tracking lived in the Landscape page component: navigate
   to Systems and back and the EventSource was torn down with the component —
   an in-flight Core BASIS sweep (which takes minutes) appeared to vanish.

   This provider lives in the root layout, so it survives route changes. The
   Shell renders one Dynamic Island from this state on every page, and any
   page can start a run or read live progress. */

import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { apiBase } from "../lib/api";

const API = apiBase();

export type TcodeState = "pending" | "running" | "done" | "warn";

export type RunState = {
  runId: string | null;
  sid: string | null;             // system currently being processed
  sids: string[];                 // all systems in this run
  phase: string;                  // human-readable current phase
  tcodes: string[];               // full checklist for the active system
  tcodeStatus: Record<string, TcodeState>;
  log: { message: string; level: string; at: number }[];
  errors: string[];
  done: boolean;
  startedAt: number | null;
};

const EMPTY: RunState = {
  runId: null, sid: null, sids: [], phase: "", tcodes: [],
  tcodeStatus: {}, log: [], errors: [], done: false, startedAt: null,
};

type Ctx = RunState & {
  startRun: (sids: string[], opts: { coreBasis: boolean; flows: boolean }) => Promise<void>;
  dismiss: () => void;
  isRunning: boolean;
};

const RunCtx = createContext<Ctx | null>(null);

export function useRun(): Ctx {
  const ctx = useContext(RunCtx);
  if (!ctx) throw new Error("useRun must be used inside <RunProvider>");
  return ctx;
}

const STORAGE_KEY = "sap_agent_active_run";

export function RunProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<RunState>({ ...EMPTY });
  const esRef = useRef<EventSource | null>(null);

  const attach = useCallback((runId: string, sids: string[]) => {
    esRef.current?.close();
    const es = new EventSource(`${API}/api/events/${runId}`);
    esRef.current = es;

    es.onmessage = e => {
      const ev = JSON.parse(e.data);
      setState(prev => {
        const next = { ...prev };
        switch (ev.type) {
          case "system_start":
            next.sid = ev.sid;
            next.phase = `${ev.sid}: logging in…`;
            next.tcodes = [];
            next.tcodeStatus = {};
            break;
          case "probe":
            next.phase = `${ev.sid}: logged in (${ev.mode} mode) — starting checks`;
            break;
          case "core_basis_start":
            next.tcodes = ev.tcodes ?? [];
            next.tcodeStatus = Object.fromEntries((ev.tcodes ?? []).map((t: string) => [t, "pending"]));
            next.phase = `${ev.sid}: Core BASIS sweep (${(ev.tcodes ?? []).length} T-codes)`;
            break;
          case "core_basis_progress": {
            next.log = [...prev.log, { message: ev.message, level: ev.level, at: Date.now() }].slice(-200);
            next.phase = ev.message;
            if (ev.tcode) {
              const st = { ...prev.tcodeStatus };
              // whichever was running becomes done once the next starts
              for (const k of Object.keys(st)) if (st[k] === "running") st[k] = "done";
              st[ev.tcode] = ev.level === "warn" ? "warn" : "running";
              next.tcodeStatus = st;
            }
            break;
          }
          case "core_basis_done": {
            const st = { ...prev.tcodeStatus };
            for (const k of Object.keys(st)) if (st[k] === "running") st[k] = "done";
            next.tcodeStatus = st;
            next.phase = `${ev.sid}: Core BASIS complete — ${ev.ok ? "clean" : `${ev.anomaly_count} anomalies`}`;
            break;
          }
          case "flow_start":
            next.phase = `${ev.sid}: running flow ${ev.flow_id}…`;
            break;
          case "flow_done":
            next.phase = `${ev.sid}: flow ${ev.flow_id} ${ev.ok ? "clean" : "found issues"}`;
            break;
          case "report_start":
            next.phase = `${ev.sid}: generating Word + PDF report…`;
            break;
          case "report_ready":
            next.phase = `${ev.sid}: report ready`;
            break;
          case "system_error":
            next.errors = [...prev.errors, `${ev.sid}: ${ev.error ?? "unknown error"}`];
            break;
          case "run_error":
            next.errors = [...prev.errors, `Run failed: ${ev.error ?? "unknown error"}`];
            break;
          case "run_done": {
            // D-032 belt-and-braces: whatever the event ordering, nothing may
            // still be "running" once the run is over.
            const st = { ...prev.tcodeStatus };
            for (const k of Object.keys(st)) if (st[k] === "running") st[k] = "done";
            next.tcodeStatus = st;
            next.done = true;
            next.runId = null;
            next.phase = "";
            es.close();
            esRef.current = null;
            try { localStorage.removeItem(STORAGE_KEY); } catch {}
            break;
          }
        }
        return next;
      });
    };

    es.onerror = () => {
      // EventSource auto-reconnects while CONNECTING; only surrender when
      // the browser has closed it for good.
      if (es.readyState !== EventSource.CLOSED) return;
      esRef.current = null;
      setState(prev => ({
        ...prev, runId: null, done: true, phase: "",
        errors: [...prev.errors, "Lost connection to the run's event stream — check the Runs page and backend logs."],
      }));
      try { localStorage.removeItem(STORAGE_KEY); } catch {}
    };
    void sids;
  }, []);

  // Re-attach to an in-flight run after a full page reload (not just a
  // client-side route change, which this provider already survives).
  useEffect(() => {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return;
      const { runId, sids, startedAt } = JSON.parse(raw);
      if (!runId) return;
      setState({ ...EMPTY, runId, sids, startedAt, phase: "Reconnecting to run…" });
      attach(runId, sids);
    } catch { /* ignore malformed storage */ }
    return () => { esRef.current?.close(); };
  }, [attach]);

  const startRun = useCallback(async (sids: string[], opts: { coreBasis: boolean; flows: boolean }) => {
    setState({ ...EMPTY, sids, phase: "Starting run…", startedAt: Date.now() });
    try {
      const res = await fetch(`${API}/api/run`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          sids,
          flows: opts.flows ? ["flows/sm37_cancelled_jobs.json"] : [],
          include_core_basis: opts.coreBasis,
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
      const body = await res.json();
      if (!body.run_id) throw new Error(`backend returned no run_id: ${JSON.stringify(body)}`);
      setState(prev => ({ ...prev, runId: body.run_id }));
      try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify({
          runId: body.run_id, sids, startedAt: Date.now(),
        }));
      } catch { /* private mode — reload recovery just won't work */ }
      attach(body.run_id, sids);
    } catch (err) {
      setState(prev => ({
        ...prev, runId: null, done: true, phase: "",
        errors: [`Could not start the run via ${API}: ${err instanceof Error ? err.message : String(err)}`],
      }));
    }
  }, [attach]);

  const dismiss = useCallback(() => setState({ ...EMPTY }), []);

  return (
    <RunCtx.Provider value={{ ...state, startRun, dismiss, isRunning: state.runId !== null }}>
      {children}
    </RunCtx.Provider>
  );
}
