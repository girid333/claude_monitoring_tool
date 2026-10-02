"use client";

/* App chrome.

   D-050: the app now has three sections behind one Sentinel TopBar —
   Monitoring (/basis + Systems/Runs/Reports/RFC), Health check (/health)
   and Record and replay (/replay, the Lab). The landing page (/) and
   /health are full-width Sentinel pages that render their own TopBar, so
   Shell steps aside for them. Every other page gets the same TopBar plus a
   sidebar listing only its own section's pages, so record/replay entry
   points (Recorder, API Flows, SAP GUI sandbox) are reachable from /replay
   and never from Monitoring's navigation.

   Original note (sidebar, D-016): ports design/mockup.html's
   macOS-Settings-style sidebar into a real, reusable layout. */

import Link from "next/link";
import { usePathname } from "next/navigation";
import { LayoutGrid, PlayCircle, Radar, FileText, Server, FlaskConical, Plug, Workflow, ListVideo } from "lucide-react";
import type { ReactNode } from "react";
import { useRun } from "./RunContext";
import TopBar from "@/components/sentinel/TopBar";
import "../sentinel.css";

const MONITORING_NAV = [
  { href: "/basis", label: "Landscape", icon: LayoutGrid },
  { href: "/systems", label: "Systems", icon: Server },
  { href: "/runs", label: "Runs", icon: PlayCircle },
  { href: "/reports", label: "Reports", icon: FileText },
  // D-045: API-based BASIS checks (RFC over SOAP) — no agent, no replay.
  { href: "/rfc", label: "RFC Checks", icon: Plug },
];

const REPLAY_NAV = [
  // D-050: flow library, step viewer, replay and data runs (was on "/").
  { href: "/replay", label: "Recordings", icon: ListVideo },
  { href: "/replay/recorder", label: "Recorder", icon: Radar },
  // D-048: compose + replay flows from discovered OData/RFC operations.
  { href: "/replay/apiflow", label: "API Flows", icon: Workflow },
  // D-044: isolated sandbox, deliberately not merged with WebGUI replay.
  { href: "/replay/sapgui", label: "SAP GUI (sandbox)", icon: FlaskConical },
  // Shared with Monitoring: systems and the report library.
  { href: "/systems", label: "Systems", icon: Server },
  { href: "/reports", label: "Reports", icon: FileText },
];

// Pages that bring their own full-width Sentinel chrome.
const BARE = (p: string) => p === "/" || p === "/health" || p.startsWith("/health/")
  || p === "/performance" || p.startsWith("/performance/");   // D-052

export default function Shell({ children }: { children: ReactNode }) {
  const pathname = usePathname() || "/";
  const run = useRun();

  if (BARE(pathname)) return <>{children}</>;

  const inReplay = pathname === "/replay" || pathname.startsWith("/replay/");
  const NAV = inReplay ? REPLAY_NAV : MONITORING_NAV;
  const TOP = 57; // .sx-top: 56px row + 1px hairline

  return (
    <>
      {/* D-050: same TopBar as the landing page. .sx scopes Sentinel's tokens
          to the bar only; minHeight/background reset because .sx is written
          for full pages. Sticky on the wrapper, since the bar's own sticky
          would be confined to this wrapper. */}
      <div className="sx" style={{ minHeight: 0, background: "none", position: "sticky", top: 0, zIndex: 40 }}>
        <TopBar />
      </div>
      <div style={{ display: "flex", minHeight: `calc(100vh - ${TOP}px)` }}>
        <aside style={{
          width: 236, flexShrink: 0, padding: "20px 12px",
          background: "rgba(18,18,21,.65)",
          backdropFilter: "blur(28px) saturate(160%)",
          WebkitBackdropFilter: "blur(28px) saturate(160%)",
          borderRight: "1px solid var(--hairline)",
          display: "flex", flexDirection: "column", gap: 2,
          position: "sticky", top: TOP, height: `calc(100vh - ${TOP}px)`,
        }}>
          <div style={{ padding: "2px 10px 12px", fontSize: 11, fontWeight: 600, letterSpacing: ".06em",
                        textTransform: "uppercase", color: "var(--text-3)" }}>
            {inReplay ? "Record and replay · Lab" : "Monitoring"}
          </div>
          <nav style={{ display: "flex", flexDirection: "column", gap: 2 }}>
            {NAV.map(({ href, label, icon: Icon }) => {
              const active = pathname === href;
              return (
                <Link key={href} href={href} className={`navlink${active ? " active" : ""}`} style={{
                  display: "flex", alignItems: "center", gap: 10,
                  padding: "7px 10px", borderRadius: "var(--r-sm)",
                  color: active ? "#fff" : "var(--text-2)",
                  background: active ? "var(--blue)" : "transparent",
                  fontSize: 13.5, fontWeight: 500, textDecoration: "none",
                }}>
                  <Icon size={16} style={{ flexShrink: 0, opacity: .9 }} />
                  {label}
                  {/* live run indicator, visible from every page (D-031) */}
                  {(href === "/basis" || href === "/replay") && run.isRunning && (
                    <span className="pulse" style={{
                      marginLeft: "auto", width: 7, height: 7, borderRadius: "50%",
                      background: active ? "#fff" : "var(--blue)",
                    }} />
                  )}
                </Link>
              );
            })}
          </nav>
          <div style={{ flex: 1 }} />
          <div style={{
            margin: "0 4px", padding: "10px 12px", borderRadius: "var(--r-md)",
            background: "rgba(255,255,255,.04)", border: "1px solid var(--hairline)",
            fontSize: 11.5, color: "var(--text-2)", lineHeight: 1.45,
          }}>
            {inReplay
              ? "Lab: record a transaction once, replay it per data row with an evidence document. Verified on SM37; other transactions are still being hardened."
              : "D-017: Core BASIS (v1, 23 T-codes) + Functional Sanity (v2 recorded flows) share one report pipeline."}
          </div>
        </aside>
        <main style={{ flex: 1, minWidth: 0, padding: "26px 40px 48px", position: "relative" }}>
          {children}
        </main>
      </div>
    </>
  );
}
