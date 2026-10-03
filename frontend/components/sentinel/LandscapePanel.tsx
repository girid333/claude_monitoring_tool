"use client";

import type { AppServerRow, CloudMeta, HanaLive, Landscape, Rating } from "@/lib/sentinelApi";
import { STATUS, StatusPill, useTip } from "./PerfCharts";

/* D-052b — application servers, the database tier (scale-out hosts + HANA
   system replication) and which app server sits closest to the database.
   Latency source is stated on screen: SMON NETRTT and the HANA client network
   trace are network measurements; ST03N's DB time per call only ranks. */

const us = (v: number | null | undefined) => v == null ? "–" : v >= 1000 ? `${(v / 1000).toFixed(2)} ms` : `${Math.round(v)} µs`;
const pct = (v: number | null | undefined) => v == null ? "–" : `${Math.round(v)} %`;
const HSR_RATING: Record<string, Rating> = { ACTIVE: "good", SYNCING: "warning", INITIALIZING: "warning", UNKNOWN: "serious", ERROR: "critical" };

function Where({ c }: { c?: CloudMeta }) {
  if (!c || (!c.zone && !c.provider)) return null;
  return <span className="ls-where">{[c.provider, c.zone, c.vm_size].filter(Boolean).join(" · ")}
    {c.placement_group ? <em title="Placement group">PG {c.placement_group}</em> : null}</span>;
}

function Placement({ s }: { s: AppServerRow }) {
  const chips: [string, boolean][] = [];
  if (s.same_host_as_db) chips.push(["Same host as DB", true]);
  if (s.same_zone_as_db !== undefined) chips.push([s.same_zone_as_db ? "Same zone as DB" : "Other zone than DB", s.same_zone_as_db]);
  if (s.same_pg_as_db) chips.push(["Same placement group", true]);
  return <>{chips.map(([t, ok]) => <span key={t} className={`ls-chip ${ok ? "ok" : "off"}`}>{ok ? "✓ " : "↔ "}{t}</span>)}</>;
}

/* App tier → DB: one bar per instance, length = latency, colour = its rating (with label). */
function LatencyBars({ app }: { app: Landscape["app"] }) {
  const t = useTip();
  const rows = app.servers.filter(s => s.latency_us != null).sort((a, b) => (a.latency_rank ?? 99) - (b.latency_rank ?? 99));
  if (!rows.length) return <p className="px-empty">No latency source for these instances yet.</p>;
  const max = Math.max(...rows.map(s => s.latency_us!), app.measured ? 700 : 0);
  return (
    <div ref={t.box} className="ls-bars" onMouseLeave={t.hide} role="list" aria-label="Application server to database latency">
      {app.measured ? <div className="ls-target" style={{ left: `calc(180px + (100% - 270px) * ${300 / max})` }} title="0.3 ms target"><span>0.3 ms</span></div> : null}
      {rows.map(s => {
        const r: Rating = app.measured ? (s.latency_rating ?? "unknown") : "unknown";
        return (
          <div key={s.name} role="listitem" className={`ls-bar ${s.nearest ? "near" : ""}`} data-testid={`ls-lat-${s.name}`}
            onMouseMove={e => t.show(e, s.name, [["Host", s.host], ["Latency", us(s.latency_us)], ["Rank", `${s.latency_rank} of ${rows.length}`],
              ...(s.cloud.zone ? [["Zone", s.cloud.zone] as [string, string]] : []),
              ["Source", app.source === "smon" ? "SMON NETRTT" : app.source === "hana_trace" ? "HANA client trace" : "ST03N DB time / call"]])}>
            <span className="ls-bar-n">{s.nearest ? <b className="ls-star" aria-label="nearest">★</b> : null}{s.name}<small>{s.host}</small></span>
            <span className="ls-bar-t"><span style={{ width: `${Math.max(2, s.latency_us! / max * 100)}%`, background: app.measured ? STATUS[r] : "#3987e5" }} /></span>
            <span className="ls-bar-v">{us(s.latency_us)}{app.measured ? <StatusPill rating={r} /> : null}</span>
          </div>
        );
      })}
      {t.view}
    </div>
  );
}

export default function LandscapePanel({ land, hanaLive, mode }: { land: Landscape; hanaLive?: HanaLive | null; mode: "live" | "day" }) {
  const app = land.app;
  const near = app.servers.find(s => s.nearest);
  const hosts = land.hana.hosts ?? [];
  const sr = land.hana.hsr;
  const live = new Map((hanaLive?.hosts ?? []).map(h => [h.host, h]));
  const cloudErr = Object.values(land.cloud).find(c => c.error && c.provider !== "none")?.error;
  return (
    <>
      <h2 className="px-h">System landscape</h2>
      <section className="px-two">
        <div className="sx-panel px-card" data-testid="ls-nearest">
          <div className="px-card-h"><h3>Closest application server to the database</h3>
            <span>{app.measured ? "network round trip" : "ranked by DB time per call"}</span></div>
          {near ? (
            <div className="ls-hero">
              <div className="ls-hero-n"><span className="ls-star">★</span><div><b data-testid="ls-nearest-name">{near.name}</b>
                <small>{near.host}{near.same_host_as_db ? " · runs on the DB host" : ""}</small><Where c={near.cloud} /></div></div>
              <div className="ls-hero-v"><b>{us(near.latency_us)}</b><span>{near.latency_us == null ? (app.count === 1 ? "only application server" : "no latency source")
                : app.measured ? "to the database" : "DB time per call"}</span></div>
              <div className="ls-hero-c"><Placement s={near} /></div>
            </div>
          ) : <p className="px-empty">No instance could be ranked.</p>}
          <LatencyBars app={app} />
          <p className="ls-src"><b>Source:</b> {app.source_label}.{app.measured ? " Targets per SAP Note 1100926: ≤ 0.3 ms good, ≤ 0.7 ms moderate." : ""}</p>
          {app.how_to_measure.length ? <ul className="ls-how">{app.how_to_measure.map(h => <li key={h}>{h}</li>)}</ul> : null}
          {cloudErr ? <p className="ls-src">Cloud placement: {cloudErr}</p> : null}
        </div>

        <div className="sx-panel px-card" data-testid="ls-db">
          <div className="px-card-h"><h3>Database tier</h3><span>{hosts.length > 1 ? `scale-out · ${hosts.length} hosts` : "single host"}{sr ? " · system replication" : ""}</span></div>
          <div className="ls-sites">
            <div className="ls-site">
              <div className="ls-site-h">{sr ? `Primary · ${sr.primary_site}` : "HANA"}</div>
              {hosts.map(h => {
                const l = live.get(h.host);
                const mem = l?.mem_limit_gb ? (l.mem_used_gb ?? 0) / l.mem_limit_gb * 100 : null;
                return (
                  <div key={h.host} className={`ls-host ${h.active ? "" : "off"}`} data-testid={`ls-host-${h.host}`}>
                    <b>{h.host}</b><span className="ls-role">{h.role || "?"}{h.active ? "" : " · inactive"}</span>
                    {mode === "live" && l ? <span className="ls-host-m">CPU {pct(l.cpu)} · mem {pct(mem)}</span> : null}
                    <Where c={land.cloud[h.host] ?? (h.platform ? { provider: h.platform, vm_size: h.model } : undefined)} />
                  </div>
                );
              })}
            </div>
            {sr ? sr.sites.map(site => (
              <div key={site.site} className="ls-site sec">
                <div className="ls-link" aria-hidden><span>{site.mode}</span></div>
                <div className="ls-site-h">Secondary · {site.site} <StatusPill rating={HSR_RATING[site.status] ?? "serious"} /></div>
                {site.hosts.map(h => <div key={h} className="ls-host"><b>{h}</b><span className="ls-role">{site.operation_mode}</span></div>)}
              </div>
            )) : null}
          </div>
          {sr ? (
            <table className="px-t" data-testid="ls-hsr"><thead><tr><th>Service</th><th>Status</th><th>Backlog</th><th>Shipping delay</th></tr></thead>
              <tbody>{sr.services.map(s => (
                <tr key={`${s.host}:${s.port}:${s.secondary_site}`}><td><b>{s.host}:{s.port}</b><small>→ {s.secondary_host} ({s.secondary_site}, {s.mode})</small></td>
                  <td><StatusPill rating={HSR_RATING[s.status] ?? "serious"} />{s.details ? <small>{s.details}</small> : null}</td>
                  <td>{s.backlog_s == null ? "–" : `${s.backlog_s} s`}<small>{s.backlog_mb} MB</small></td>
                  <td>{s.shipping_delay_s == null ? "–" : `${s.shipping_delay_s} s`}</td></tr>))}</tbody></table>
          ) : <p className="ls-src">No HANA system replication configured{hosts.length ? "" : " (or HANA not reachable)"}.</p>}
          {mode === "day" && land.hana.takeovers?.length ? (
            <p className="ls-src" data-testid="ls-takeover"><b>Takeover that day:</b> {land.hana.takeovers.map(t => `${t.start.slice(11, 16)} ${t.from_site} → ${t.to_site} (${t.type})`).join(" · ")}</p>) : null}
          {mode === "day" ? <p className="ls-src">Hosts and replication show the current state.</p> : null}
        </div>
      </section>

      <section className="sx-panel px-card ls-apps" data-testid="ls-apps">
        <div className="px-card-h"><h3>Application servers</h3><span>{app.active} of {app.count} active · per instance</span></div>
        <div className="ls-scroll">
          <table className="px-t">
            <thead><tr><th>Instance</th><th>Placement</th><th>Dialog WPs</th><th>CPU</th><th>Dialog resp.</th><th>DB time / call</th><th>RTT (SMON)</th><th>RTT (HANA trace)</th></tr></thead>
            <tbody>{app.servers.map(s => (
              <tr key={s.name} className={s.nearest ? "ls-near" : ""} data-testid={`ls-app-${s.name}`}>
                <td><b>{s.nearest ? "★ " : ""}{s.name}</b><small>{s.host}{s.local ? " · this instance" : ""}{s.historic ? " · not running now" : !s.active ? " · inactive" : ""}</small></td>
                <td><Where c={s.cloud} /><Placement s={s} /></td>
                <td>{s.total_dia ? `${s.busy_dia} / ${s.total_dia}` : "–"}</td>
                <td>{pct(s.cpu)}{s.os_source ? <small>{s.os_source}</small> : null}</td>
                <td>{s.dia_resp_ms == null ? "–" : `${s.dia_resp_ms.toLocaleString()} ms`}<small>{s.dia_steps ? `${s.dia_steps.toLocaleString()} steps` : ""}</small></td>
                <td>{us(s.db_us_per_call)}</td><td>{us(s.rtt_smon_us)}</td><td>{us(s.rtt_hana_us)}</td>
              </tr>))}</tbody>
          </table>
        </div>
      </section>
    </>
  );
}
