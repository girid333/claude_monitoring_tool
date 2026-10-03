"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { healthApi, perfApi } from "@/lib/sentinelApi";
import type { PerfKpi, PerfRanges, PerfView, Rating, SystemItem } from "@/lib/sentinelApi";
import LandscapePanel from "./LandscapePanel";
import SmonScheduler from "./SmonScheduler";
import { HourlyStack, Meter, PctLines, ScoreDial, SERIES, Spark, STATUS, STATUS_LABEL, StatusPill, WpBars } from "./PerfCharts";

/* D-052 — SAP + HANA performance. Live status (manual refresh, optional auto
   refresh every 30/60 s) or one day from SAP's own history (as far back as
   SAP keeps it, at most 90 days). Exports build from the exact view shown. */

const fmt = (v: number | string | null | undefined, unit = "") =>
  v == null ? "–" : typeof v === "string" ? v : `${v.toLocaleString(undefined, { maximumFractionDigits: v < 10 ? 2 : v < 1000 ? 1 : 0 })}${unit ? ` ${unit}` : ""}`;

const HEADLINE: Record<Rating, string> = {
  good: "Performing well", warning: "Mostly healthy, a few figures to watch", serious: "Performance needs attention",
  critical: "Performance problem", unknown: "Not enough data to rate",
};

function KpiTile({ k }: { k: PerfKpi }) {
  return (
    <div className={`px-kpi px-r-${k.rating}`} data-testid={`px-kpi-${k.id}`}>
      <div className="px-kpi-top"><span className="px-kpi-l">{k.label}</span><StatusPill rating={k.rating} /></div>
      <div className="px-kpi-v">{fmt(k.value)}<span>{k.unit}</span></div>
      <div className="px-kpi-foot">
        <div className="px-kpi-d">{k.detail}{k.target ? <em>Target {k.target}</em> : null}</div>
        <Spark data={k.spark} color={k.group === "HANA" ? SERIES.db : SERIES.cpu} />
      </div>
    </div>
  );
}

export default function PerfDashboard() {
  const [systems, setSystems] = useState<SystemItem[]>([]);
  const [sid, setSid] = useState("");
  const [mode, setMode] = useState<"live" | "day">("live");
  const [date, setDate] = useState("");
  const [ranges, setRanges] = useState<PerfRanges | null>(null);
  const [view, setView] = useState<PerfView | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [auto, setAuto] = useState(0);
  const [declinedFor, setDeclinedFor] = useState<Record<string, boolean>>({});   // "use read-only sources", per system
  const [exporting, setExporting] = useState<string | null>(null);
  const [lastLoad, setLastLoad] = useState<Date | null>(null);
  const inFlight = useRef(false);
  const seq = useRef(0);

  useEffect(() => {
    healthApi.systems().then(xs => { setSystems(xs); if (xs[0]) setSid(s => s || xs[0].sid); }).catch(e => setError(String(e.message || e)));
  }, []);

  useEffect(() => {
    if (!sid) return;
    let gone = false;                               // a slower answer for the previous system must not land
    setRanges(null);
    perfApi.ranges(sid).then(r => {
      if (gone) return;
      setRanges(r);
      // newest day SAP still holds inside the analysis window (F4H's ST03N days are older than 90 days)
      const lim = new Date(r.today); lim.setDate(lim.getDate() - r.limit_days);
      const lo = lim.toISOString().slice(0, 10);
      const yest = new Date(r.today); yest.setDate(yest.getDate() - 1);
      setDate(d => d || r.workload_days.find(x => x < r.today && x >= lo) || yest.toISOString().slice(0, 10));
    }).catch(() => { if (!gone) setRanges(null); });
    return () => { gone = true; };
  }, [sid]);

  const dayKey = mode === "day" ? date : "";        // a date change must not re-read the LIVE view

  const load = useCallback(async (quiet = false) => {
    if (!sid) return;
    if (mode === "day" && !dayKey) { ++seq.current; inFlight.current = false; setBusy(null); return; }
    if (quiet && inFlight.current) return;          // auto refresh never stacks requests
    const ticket = ++seq.current;                   // the latest request wins (system/date/mode changes)
    inFlight.current = true;
    if (!quiet) setBusy(mode === "live" ? `Reading live performance data from ${sid}…` : `Analysing ${dayKey} in ${sid}…`);
    if (!quiet) setError(null);
    try {
      const v = mode === "live" ? await perfApi.live(sid) : await perfApi.day(sid, dayKey);
      if (ticket !== seq.current) return;
      setView(v); setLastLoad(new Date()); setError(null);
    } catch (e) {
      if (ticket !== seq.current) return;
      // a failed auto refresh keeps the last good view on screen
      setError((quiet ? "Auto refresh failed — showing the previous reading: " : "") + String((e as Error).message || e));
      if (!quiet) setView(null);
    } finally {
      if (ticket === seq.current) { inFlight.current = false; setBusy(null); }
    }
  }, [sid, mode, dayKey]);
  const loadRef = useRef(load);
  loadRef.current = load;                           // callbacks from long writes reload the CURRENT selection

  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    if (!auto || mode !== "live") return;
    const t = setInterval(() => load(true), auto * 1000);
    return () => clearInterval(t);
  }, [auto, mode, load]);

  const sap = view?.kpis.filter(k => k.group === "SAP") ?? [];
  const hana = view?.kpis.filter(k => k.group === "HANA") ?? [];
  const minDate = useMemo(() => {
    if (!ranges) return undefined;
    const lim = new Date(ranges.today); lim.setDate(lim.getDate() - ranges.limit_days);
    const lo = ranges.oldest && ranges.oldest > lim.toISOString().slice(0, 10) ? ranges.oldest : lim.toISOString().slice(0, 10);
    return lo;
  }, [ranges]);
  const dayKnown = !ranges || mode === "live" || ranges.workload_days.includes(date) || (ranges.hana_from ?? "9") <= date;
  const hanaHours = view?.mode === "day" ? view.hana_hours ?? [] : view?.hana?.today ?? [];
  const smon = view?.smon;

  const exportAs = async (f: "xlsx" | "pdf") => {
    if (!view || exporting) return;
    setExporting(f === "pdf" ? "Building the PDF report…" : "Building the Excel workbook…");
    try { await perfApi.exportView(view, f); } catch (e) { setError(String((e as Error).message || e)); } finally { setExporting(null); }
  };
  const smonDeclined = !!declinedFor[sid];



  return (
    <main className="sx-wrap px">
      <div className="sx-page-head">
        <div>
          <h1 className="sx-h2">Performance</h1>
          <p className="sx-sub">Live SAP and HANA performance, or any day SAP still keeps in its own history (ST03N, SMON, HANA statistics).</p>
        </div>
        <div className="sx-tools">
          {systems.length ? (
            <select className="sx-select" aria-label="SAP system" value={sid} onChange={e => { setSid(e.target.value); setView(null); }} data-testid="px-sid">
              {systems.map(s => <option key={s.sid} value={s.sid}>{s.sid}{s.system_name && s.system_name !== s.sid ? ` — ${s.system_name}` : ""}</option>)}
            </select>
          ) : null}
          <div className="px-seg" role="tablist" aria-label="View">
            <button role="tab" aria-selected={mode === "live"} onClick={() => setMode("live")} data-testid="px-mode-live">Live</button>
            <button role="tab" aria-selected={mode === "day"} onClick={() => { setMode("day"); setAuto(0); }} data-testid="px-mode-day">Day analysis</button>
          </div>
        </div>
      </div>

      <div className="px-bar sx-panel">
        {mode === "day" ? (
          <label className="px-date">Date
            <input type="date" value={date} min={minDate} max={ranges?.today} onChange={e => setDate(e.target.value)} data-testid="px-date" />
          </label>
        ) : (
          <label className="px-date">Auto refresh
            <select className="sx-select px-small" value={auto} onChange={e => setAuto(Number(e.target.value))} data-testid="px-auto">
              <option value={0}>Off</option><option value={30}>Every 30 s</option><option value={60}>Every 60 s</option>
            </select>
          </label>
        )}
        <button className="sx-btn" onClick={() => load()} disabled={!!busy || !sid} data-testid="px-refresh">↻ Refresh</button>
        <span className="px-status">
          {mode === "live" && auto ? <><span className="px-pulse" aria-hidden />Live · every {auto} s</> : null}
          {lastLoad ? <span>Updated {lastLoad.toLocaleTimeString()}</span> : null}
          {ranges ? <span title={`Oldest day SAP still holds: workload ${ranges.workload_days.at(-1) ?? "none"}, HANA ${ranges.hana_from ?? "none"} · analysis window ${ranges.limit_days} days`}>Analysable from {minDate}</span> : null}
        </span>
        <span className="px-exp">
          <button className="sx-btn" onClick={() => exportAs("xlsx")} disabled={!view || !!exporting} data-testid="px-xlsx">Export Excel</button>
          <button className="sx-btn sx-btn-primary" onClick={() => exportAs("pdf")} disabled={!view || !!exporting} data-testid="px-pdf">Export PDF report</button>
        </span>
      </div>

      {error ? <p className="sx-error" role="alert">{error}</p> : null}
      {busy || exporting ? <p className="sx-busy" aria-live="polite">{busy ?? exporting}</p> : null}
      {mode === "day" && !dayKnown ? <p className="px-note">SAP keeps no workload or HANA history for {date}; pick a date from {minDate}{ranges?.workload_days.length && ranges.workload_days[0] < (minDate ?? "") ? ` (ST03N workload ends ${ranges.workload_days[0]}, before the ${ranges.limit_days}-day window — check job SAP_COLLECTOR_FOR_PERFMONITOR)` : ""}.</p> : null}
      {!view && !busy && !error ? (systems.length ? (mode === "day" && !date
        ? <div className="sx-drop"><h3>Pick a date</h3><p>Choose the day to analyse.</p></div> : null)
        : <div className="sx-drop"><h3>Pick a system</h3><p>Add an SAP system with its user on the Systems page first.</p></div>) : null}

      {view ? (
        <>
          <section className="sx-panel px-hero" aria-label="Overall">
            <ScoreDial score={view.overall.score} grade={view.overall.grade} rating={view.overall.rating} />
            <div className="px-hero-t">
              <div className="px-eyebrow">{view.mode === "live" ? "Live status" : `Day analysis · ${view.date}`}</div>
              <h2 data-testid="px-headline">{HEADLINE[view.overall.rating]}</h2>
              <div className="px-counts">
                {(["critical", "serious", "warning", "good"] as Rating[]).map(r => (
                  <span key={r} className="px-count"><i style={{ background: STATUS[r] }} />{view.overall.counts[r] ?? 0} {STATUS_LABEL[r].toLowerCase()}</span>
                ))}
              </div>
              <dl className="px-facts">
                <div><dt>System</dt><dd>{view.sid} · client {view.client}</dd></div>
                <div><dt>Release</dt><dd>{view.release || "–"} · {view.db || "–"}</dd></div>
                <div><dt>Host</dt><dd>{view.host || "–"}</dd></div>
                <div><dt>SAP time</dt><dd>{view.as_of.slice(0, 16)}</dd></div>
              </dl>
            </div>
            {view.mode === "live" && view.sap ? (
              <div className="px-hero-m">
                <Meter label="Dialog WPs busy" value={view.sap.dia_util} rating={view.kpis.find(k => k.id === "dia_util")?.rating ?? "unknown"} />
                <Meter label={view.kpis.find(k => k.id === "host_cpu")?.label ?? "App server CPU"}
                  value={(view.kpis.find(k => k.id === "host_cpu")?.value as number | null) ?? view.sap.cpu.util} rating={view.kpis.find(k => k.id === "host_cpu")?.rating ?? "unknown"} />
                <Meter label="HANA memory" value={(view.kpis.find(k => k.id === "hana_mem")?.value as number | null) ?? null}
                  rating={view.kpis.find(k => k.id === "hana_mem")?.rating ?? "unknown"} />
                <div className="px-mini">
                  <div><b>{fmt(view.sap.users)}</b><span>{(view.landscape?.app.count ?? 1) > 1 ? "users here" : "users"}</span></div>
                  <div><b>{fmt(view.sap.jobs_running)}</b><span>jobs running</span></div>
                  <div><b>{fmt(view.sap.locks)}</b><span>locks</span></div>
                </div>
              </div>
            ) : null}
          </section>

          {view.findings.length ? (
            <section className="px-find" aria-label="Findings">
              {view.findings.slice(0, 4).map(f => (
                <div key={f.kpi} className={`sx-panel px-find-c px-r-${f.rating}`}>
                  <StatusPill rating={f.rating} />
                  <b>{f.title}</b>
                  <p>{f.recommendation}</p>
                </div>
              ))}
            </section>
          ) : null}

          {view.landscape ? <LandscapePanel land={view.landscape} hanaLive={view.hana} mode={view.mode} /> : null}

          <h2 className="px-h">SAP application server</h2>
          <section className="px-grid">{sap.map(k => <KpiTile key={k.id} k={k} />)}</section>

          <section className="px-two">
            <div className="sx-panel px-card px-wide">
              <div className="px-card-h"><h3>Dialog response time {view.mode === "live" ? "· last 24 hours" : `· ${view.date}`}</h3>
                <span>ms per dialog step · ST03N</span></div>
              {view.hourly.some(h => h.dia_steps) ? <HourlyStack data={view.hourly} /> :
                <p className="px-empty">No dialog workload recorded for this period.</p>}
            </div>
          </section>

          {view.mode === "live" && view.sap ? (
            <section className="px-two">
              <div className="sx-panel px-card">
                <div className="px-card-h"><h3>Work processes</h3><span>{view.sap.servers.length} instance(s) · SM50</span></div>
                <WpBars wp={view.sap.wp} />
              </div>
              <div className="sx-panel px-card">
                <div className="px-card-h"><h3>Buffers</h3><span>ST02 · worst hit ratio {fmt(view.sap.buffers?.worst_hit, "%")}</span></div>
                <div className="px-buf">
                  {[...(view.sap.buffers?.buffers ?? [])]
                    // rated buffers first, worst hit ratio first — the ones the KPI is about
                    .sort((a, b) => Number(!!b.rated) - Number(!!a.rated) || (a.hit ?? 101) - (b.hit ?? 101)).slice(0, 8).map(b => (
                    <Meter key={b.name} label={b.name} value={b.hit}
                      rating={b.hit == null || !b.rated ? "unknown" : b.hit >= 98 ? "good" : b.hit >= 95 ? "warning" : "serious"}
                      sub={`${b.swaps} swaps · ${fmt(b.free_pct, "%")} free${b.rated ? "" : " · not rated"}`} />
                  ))}
                </div>
              </div>
            </section>
          ) : null}

          <section className="px-two">
            <div className="sx-panel px-card">
              <div className="px-card-h"><h3>Top transactions</h3><span>by total response time</span></div>
              {view.workload?.top.length ? (
                <table className="px-t"><thead><tr><th>Transaction / program</th><th>Steps</th><th>Avg ms</th><th>Share</th></tr></thead>
                  <tbody>{view.workload.top.slice(0, 10).map(t => (
                    <tr key={t.name}><td><b>{t.name}</b><small>{[t.task, t.users == null ? null : `${t.users} user(s)`].filter(Boolean).join(" · ")}</small></td><td>{t.steps.toLocaleString()}</td>
                      <td>{fmt(t.avg_resp)}</td><td><div className="px-share"><div style={{ width: `${t.share}%` }} /></div>{t.share}%</td></tr>))}
                  </tbody></table>) : <p className="px-empty">No transaction detail in SAP's workload data for this period.</p>}
            </div>
            <div className="sx-panel px-card">
              <div className="px-card-h"><h3>Workload by task type</h3><span>ms per step</span></div>
              {view.workload?.tasks.length ? (
                <table className="px-t"><thead><tr><th>Task type</th><th>Steps</th><th>Resp</th><th>CPU</th><th>DB</th></tr></thead>
                  <tbody>{view.workload.tasks.slice(0, 9).map(t => (
                    <tr key={t.code}><td><b>{t.name}</b></td><td>{t.steps.toLocaleString()}</td><td>{fmt(t.avg_resp)}</td>
                      <td>{fmt(t.avg_cpu)}</td><td>{fmt(t.avg_db)}</td></tr>))}</tbody></table>) :
                <p className="px-empty">No workload recorded for this period (normal on an idle or test system).</p>}
            </div>
          </section>

          <section className="sx-panel px-card px-smon" aria-label="SMON">
            <div className="px-card-h"><h3>Snapshot monitoring</h3><span>/SDF/{smon?.tool ?? "SMON"}</span></div>
            {smon?.series?.length ? (
              <div className="px-smon-grid">
                {([["act_dia", "Active dialog WPs"], ["diaq", "Dialog queue"], ["users", "Users"], ["cpu", "CPU %"]] as const).map(([k, n]) => (
                  <div key={k} className="px-smon-c"><span>{n}</span>
                    <b>{fmt(smon.series![smon.series!.length - 1][k])}</b>
                    <Spark data={smon.series!.map(p => p[k])} w={220} h={44} color={k === "diaq" ? SERIES.db : SERIES.cpu} /></div>
                ))}
              </div>
            ) : view.mode === "day" || smon?.installed === false ? (
              <p className="px-empty">{smon?.installed === false ? (smon.why || "Snapshot monitoring could not be read.") : "No snapshot monitoring data for this day."}</p>
            ) : null}
            {view.mode === "live" && smon?.installed !== false ? (
              <SmonScheduler key={sid} sid={sid} smon={smon} onChanged={() => loadRef.current(true)} declined={smonDeclined}
                onDecline={() => setDeclinedFor(d => ({ ...d, [sid]: true }))} />
            ) : null}
          </section>

          <h2 className="px-h">SAP HANA database</h2>
          <section className="px-grid">{hana.map(k => <KpiTile key={k.id} k={k} />)}</section>
          <section className="px-two">
            <div className="sx-panel px-card">
              <div className="px-card-h"><h3>HANA CPU and memory</h3><span>{view.mode === "live" ? "today" : view.date} · hourly max</span></div>
              {hanaHours.length > 1 ? (() => {
                // one slot per hour; hours HANA did not record (system down) stay gaps, not a straight line
                const last = view.mode === "live" ? Math.max(...hanaHours.map(h => h.hour)) : 23;
                const slots = Array.from({ length: last + 1 }, (_, i) => hanaHours.find(h => h.hour === i) ?? null);
                return <PctLines labels={slots.map((_, i) => String(i).padStart(2, "0"))}
                  series={[{ name: "CPU max %", color: SERIES.cpu, data: slots.map(h => h ? h.cpu_max : null) },
                           { name: "Memory used % of limit", color: SERIES.db,
                             data: slots.map(h => h && h.limit_gb ? Math.round(h.mem_max_gb / h.limit_gb * 1000) / 10 : null) }]} />;
              })() : <p className="px-empty">No HANA load history for this period.</p>}
            </div>
            <div className="sx-panel px-card">
              <div className="px-card-h"><h3>Top SQL statements</h3><span>by total execution time</span></div>
              {view.hana?.top_sql?.length ? (
                <table className="px-t"><thead><tr><th>Statement</th><th>Exec.</th><th>Avg ms</th><th>Share</th></tr></thead>
                  <tbody>{view.hana.top_sql.slice(0, 8).map((q, i) => (
                    <tr key={`${q.hash}-${i}`}><td><code>{q.statement}</code></td><td>{q.executions.toLocaleString()}</td><td>{fmt(q.avg_ms)}</td>
                      <td><div className="px-share"><div style={{ width: `${q.share}%`, background: SERIES.db }} /></div>{q.share}%</td></tr>))}
                  </tbody></table>) : <p className="px-empty">{view.mode === "day" ? "Top SQL is shown for the live view (current plan cache)." : "No SQL plan cache data."}</p>}
            </div>
          </section>

          {view.notes.length ? (
            <details className="px-notes"><summary>{view.notes.length} source(s) returned no data</summary>
              <ul>{view.notes.map((n, i) => <li key={i}>{n}</li>)}</ul></details>
          ) : null}
          <p className="sx-foot">Read over SOAP RFC in {view.took_s} s ({view.calls} calls). Ratings follow SAP EarlyWatch guidance; every rating is shown with its label.</p>
        </>
      ) : null}
    </main>
  );
}
