"use client";

/* Sidebar chrome — ports design/mockup.html's macOS-Settings-style sidebar
   (D-016) into a real, reusable layout so /,/runs,/recorder,/reports are
   actually navigable. Previously only Landscape (page.tsx) existed with no
   sidebar at all, so the other three pages were unreachable even once
   built — this is the piece that makes them a real app. */

import Link from "next/link";
import { usePathname } from "next/navigation";
import { LayoutGrid, PlayCircle, Radar, FileText, Server } from "lucide-react";
import type { ReactNode } from "react";
import { useRun } from "./RunContext";

const NAV = [
  { href: "/", label: "Landscape", icon: LayoutGrid },
  { href: "/systems", label: "Systems", icon: Server },
  { href: "/runs", label: "Runs", icon: PlayCircle },
  { href: "/recorder", label: "Recorder", icon: Radar },
  { href: "/reports", label: "Reports", icon: FileText },
];

export default function Shell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const run = useRun();

  return (
    <div style={{ display: "flex", minHeight: "100vh" }}>
      <aside style={{
        width: 236, flexShrink: 0, padding: "20px 12px",
        background: "rgba(18,18,21,.65)",
        backdropFilter: "blur(28px) saturate(160%)",
        WebkitBackdropFilter: "blur(28px) saturate(160%)",
        borderRight: "1px solid var(--hairline)",
        display: "flex", flexDirection: "column", gap: 2,
        position: "sticky", top: 0, height: "100vh",
      }}>
        <div style={{ display: "flex", gap: 8, padding: "2px 8px 18px" }}>
          <span style={{ width: 12, height: 12, borderRadius: "50%", background: "#FF5F57" }} />
          <span style={{ width: 12, height: 12, borderRadius: "50%", background: "#FEBC2E" }} />
          <span style={{ width: 12, height: 12, borderRadius: "50%", background: "#28C840" }} />
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "6px 8px 16px" }}>
          <div style={{
            width: 34, height: 34, borderRadius: 9,
            background: "linear-gradient(145deg,#0A84FF,#0055D4)",
            display: "grid", placeItems: "center", fontSize: 15, fontWeight: 700,
            color: "#fff", boxShadow: "0 4px 14px rgba(10,132,255,.35)",
          }}>S</div>
          <div>
            <div style={{ fontSize: 14, fontWeight: 600, letterSpacing: "-.01em" }}>SAP Sentinel</div>
            <div style={{ fontSize: 11, color: "var(--text-3)" }}>Monitoring Agent v2</div>
          </div>
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
                {href === "/" && run.isRunning && (
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
          D-017: Core BASIS (v1, 23 T-codes) + Functional Sanity (v2 recorded
          flows) share one report pipeline.
        </div>
      </aside>
      <main style={{ flex: 1, minWidth: 0, padding: "26px 40px 48px", position: "relative" }}>
        {children}
      </main>
    </div>
  );
}
