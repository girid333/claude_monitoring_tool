"use client";

import { useEffect, useState } from "react";
import type { Summary } from "@/lib/sentinelApi";

/* Two concentric activity-style rings: outer = checks SAP passed, inner =
   checks needing no action after Sentinel's triage. The gap between them is
   the noise Sentinel removed. Animates once on mount. */
export default function Rings({ summary, size = 260 }: { summary: Summary | null; size?: number }) {
  const [shown, setShown] = useState(false);
  useEffect(() => { const t = requestAnimationFrame(() => setShown(true)); return () => cancelAnimationFrame(t); }, []);

  const stroke = Math.round(size * 0.085);
  const gap = Math.round(stroke * 0.35);
  const r1 = size / 2 - stroke / 2 - 2;
  const r2 = r1 - stroke - gap;
  const total = summary?.total || 0;
  const outer = total ? summary!.sap_ok / total : 0;
  const inner = total ? (summary!.ok + summary!.expected) / total : 0;
  const act = summary?.act ?? 0;

  const ring = (r: number, frac: number, color: string, track: string) => {
    const c = 2 * Math.PI * r;
    return (
      <g>
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke={track} strokeWidth={stroke} />
        <circle className="sx-arc" cx={size / 2} cy={size / 2} r={r} fill="none" stroke={color}
          strokeWidth={stroke} strokeLinecap="round" strokeDasharray={c}
          strokeDashoffset={shown ? c * (1 - frac) : c}
          transform={`rotate(-90 ${size / 2} ${size / 2})`} />
      </g>
    );
  };

  const label = summary
    ? `${summary.sap_ok} of ${total} checks passed SAP's check; ${summary.ok + summary.expected} of ${total} need no action after triage; ${act} need action.`
    : "No health check imported yet.";

  return (
    <div className="sx-rings" style={{ width: size, height: size }} role="img" aria-label={label}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
        {ring(r1, outer, "#0A84FF", "rgba(10,132,255,0.16)")}
        {ring(r2, inner, "#30D158", "rgba(48,209,88,0.16)")}
      </svg>
      <div className="sx-rings-center">
        {summary ? (
          <div>
            <div className="sx-rings-big" style={{ color: act ? "#FF453A" : "#30D158" }}>{act || "✓"}</div>
            <div className="sx-rings-small">{act === 1 ? "needs action" : act ? "need action" : "nothing to act on"}</div>
          </div>
        ) : (
          <div className="sx-rings-small" style={{ maxWidth: size * 0.45 }}>No health check yet</div>
        )}
      </div>
    </div>
  );
}

export function RingsLegend({ summary }: { summary: Summary }) {
  return (
    <div className="sx-legend">
      <div className="sx-legend-row"><span className="sx-swatch" style={{ background: "#0A84FF" }} />
        <span>Passed SAP&apos;s check</span><b>{summary.sap_ok} of {summary.total}</b></div>
      <div className="sx-legend-row"><span className="sx-swatch" style={{ background: "#30D158" }} />
        <span>No action needed after triage</span><b>{summary.ok + summary.expected} of {summary.total}</b></div>
      <div className="sx-legend-row"><span className="sx-swatch" style={{ background: "#FF453A" }} />
        <span>Needs action</span><b>{summary.act}</b></div>
      <div className="sx-legend-row"><span className="sx-swatch" style={{ background: "#FF9F0A" }} />
        <span>Worth a review</span><b>{summary.review}</b></div>
    </div>
  );
}
