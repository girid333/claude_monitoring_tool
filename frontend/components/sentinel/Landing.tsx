"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { healthApi, type HealthReport } from "@/lib/sentinelApi";
import Rings, { RingsLegend } from "./Rings";
import { ROUTES } from "./TopBar";

export default function Landing() {
  const [latest, setLatest] = useState<HealthReport | null | undefined>(undefined);
  useEffect(() => { healthApi.latest().then(setLatest).catch(() => setLatest(null)); }, []);

  return (
    <main className="sx-wrap">
      <section className="sx-hero">
        <div>
          <h1 className="sx-h1">Know which SAP alerts actually need you.</h1>
          <p className="sx-lede">
            Sentinel captures BASIS monitoring evidence through WebGUI, reads SAP&apos;s own
            health checks, and separates real problems from by-design noise, with the
            evidence one click away.
          </p>
          <div className="sx-actions">
            <Link className="sx-btn sx-btn-primary" href={ROUTES.basis}>Run a monitoring report</Link>
            <Link className="sx-btn" href={ROUTES.health}>Import a health check</Link>
          </div>
        </div>

        <div className="sx-rings-card" aria-live="polite">
          <Rings summary={latest?.summary ?? null} size={280} />
          {latest ? (
            <>
              <RingsLegend summary={latest.summary} />
              <Link className="sx-link" href={`${ROUTES.health}?id=${latest.id}`}>
                Latest: {latest.system.sid || "Unknown system"} {latest.system.client}, checked {latest.report_date ?? "on an unknown date"}
              </Link>
            </>
          ) : latest === null ? (
            <p className="sx-sub" style={{ textAlign: "center", maxWidth: 30 + "ch" }}>
              Import a health check to see how many flagged checks really need action.
            </p>
          ) : null}
        </div>
      </section>

      <section className="sx-dest" aria-label="Workspaces">
        <Link href={ROUTES.basis} className="sx-panel sx-dest-card">
          <span className="sx-glyph" style={{ background: "rgba(10,132,255,0.16)" }} aria-hidden>
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#0A84FF" strokeWidth="2" strokeLinecap="round"><rect x="3" y="4" width="18" height="12" rx="2" /><path d="M8 20h8M12 16v4M7 12l3-3 3 2 4-4" /></svg>
          </span>
          <h2>BASIS monitoring report</h2>
          <p>Log on to each system through WebGUI, capture the monitoring transactions with screenshots and anomaly checks, and get the Word and PDF report.</p>
          <div className="sx-dest-foot"><span className="sx-dest-go">Open monitoring</span></div>
        </Link>

        <Link href={ROUTES.health} className="sx-panel sx-dest-card">
          <span className="sx-glyph" style={{ background: "rgba(48,209,88,0.14)" }} aria-hidden>
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#30D158" strokeWidth="2" strokeLinecap="round"><circle cx="12" cy="12" r="8" /><circle cx="12" cy="12" r="4" /></svg>
          </span>
          <h2>Health check dashboard</h2>
          <p>Pull SAP&apos;s Automated Health Check straight from the system, or import its workbook. Every flagged check is re-read against its evidence tab and marked as needing action, worth a review, or expected behaviour.</p>
          <div className="sx-dest-foot">
            <span className="sx-dest-go">Open dashboard</span>
            {latest ? <span className={`sx-tag ${latest.summary.act ? "sx-t-act" : "sx-t-ok"}`}>{latest.summary.act} to act on</span> : null}
          </div>
        </Link>
      </section>

      <Link href={ROUTES.replay} className="sx-panel sx-labrow">
        <span className="sx-glyph" style={{ background: "rgba(255,159,10,0.13)" }} aria-hidden>
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#FF9F0A" strokeWidth="2" strokeLinecap="round"><circle cx="12" cy="12" r="8" /><circle cx="12" cy="12" r="3" fill="#FF9F0A" /></svg>
        </span>
        <div>
          <strong>Record and replay</strong>{" "}<span className="sx-lab">Lab</span>
          <p>Record a transaction once and replay it with new data. Verified on SM37; other transactions are still being hardened.</p>
        </div>
        <span className="sx-dest-go">Open the lab</span>
      </Link>

      <p className="sx-foot">Sentinel for SAP BASIS</p>
    </main>
  );
}
