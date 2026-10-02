"use client";

import { useEffect, useRef, useState } from "react";
import type { HourPoint, Rating } from "@/lib/sentinelApi";

/* D-052 charts. Palette = the validated dark categorical order (dataviz
   skill, checked against Sentinel's #141417 surface: CVD ΔE ≥ 8.4, contrast
   ≥ 3:1). Status colours are reserved for ratings and always ship with an
   icon + label. Single y-axis per chart; values in text use text tokens. */
export const SERIES = { cpu: "#3987e5", db: "#d95926", wait: "#199e70", other: "#c98500" } as const;
export const STATUS: Record<Rating, string> = {
  good: "#0ca30c", warning: "#fab219", serious: "#ec835a", critical: "#d03b3b", unknown: "#6b6b70",
};
export const STATUS_LABEL: Record<Rating, string> = {
  good: "Good", warning: "Warning", serious: "Serious", critical: "Critical", unknown: "No data",
};
const ICON: Record<Rating, string> = { good: "✓", warning: "!", serious: "!!", critical: "✕", unknown: "–" };

export function StatusPill({ rating }: { rating: Rating }) {
  return (
    <span className={`px-pill px-${rating}`}>
      <span aria-hidden className="px-pill-ic" style={{ background: STATUS[rating] }}>{ICON[rating]}</span>
      {STATUS_LABEL[rating]}
    </span>
  );
}

/* ── tooltip ── */
type Tip = { x: number; y: number; lines: [string, string, string?][]; title: string } | null;

function useTip() {
  const [tip, setTip] = useState<Tip>(null);
  const box = useRef<HTMLDivElement>(null);
  const show = (e: React.MouseEvent, title: string, lines: [string, string, string?][]) => {
    const r = box.current?.getBoundingClientRect();
    if (!r) return;
    setTip({ x: e.clientX - r.left, y: e.clientY - r.top, title, lines });
  };
  const view = tip ? (
    <div className="px-tip" role="status" style={{
      left: Math.min(tip.x + 14, (box.current?.clientWidth || 600) - 210), top: Math.max(tip.y - 10, 0),
    }}>
      <div className="px-tip-t">{tip.title}</div>
      {tip.lines.map(([k, v, c]) => (
        <div key={k} className="px-tip-r">
          {c ? <i style={{ background: c }} /> : <i style={{ background: "transparent" }} />}
          <span>{k}</span><b>{v}</b>
        </div>
      ))}
    </div>
  ) : null;
  return { box, show, hide: () => setTip(null), view };
}

/* ── score dial ── */
export function ScoreDial({ score, grade, rating, size = 196 }: { score: number | null; grade: string; rating: Rating; size?: number }) {
  const [on, setOn] = useState(false);
  useEffect(() => { const t = requestAnimationFrame(() => setOn(true)); return () => cancelAnimationFrame(t); }, [score]);
  const stroke = 16, r = size / 2 - stroke, c = 2 * Math.PI * r * 0.75;
  const frac = score == null ? 0 : score / 100;
  return (
    <div className="px-dial" style={{ width: size, height: size }} role="img"
      aria-label={`Performance score ${score ?? "unknown"} of 100, grade ${grade}, overall ${STATUS_LABEL[rating]}`}>
      <svg width={size} height={size}>
        <g transform={`rotate(135 ${size / 2} ${size / 2})`}>
          <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="rgba(255,255,255,0.08)" strokeWidth={stroke}
            strokeDasharray={`${c} ${2 * Math.PI * r}`} strokeLinecap="round" />
          <circle className="sx-arc" cx={size / 2} cy={size / 2} r={r} fill="none" stroke={STATUS[rating]} strokeWidth={stroke}
            strokeDasharray={`${c} ${2 * Math.PI * r}`} strokeDashoffset={on ? c * (1 - frac) : c} strokeLinecap="round"
            style={{ filter: `drop-shadow(0 0 10px ${STATUS[rating]}66)` }} />
        </g>
      </svg>
      <div className="px-dial-c">
        <div className="px-dial-g" style={{ color: STATUS[rating] }}>{grade}</div>
        <div className="px-dial-s">{score ?? "–"}<span>/100</span></div>
      </div>
    </div>
  );
}

/* ── sparkline ── */
export function Spark({ data, color = "#3987e5", w = 120, h = 32 }: { data: number[]; color?: string; w?: number; h?: number }) {
  if (!data || data.length < 2) return null;
  const max = Math.max(...data, 1), min = Math.min(...data, 0);
  const pts = data.map((v, i) => [i / (data.length - 1) * (w - 4) + 2, h - 3 - (v - min) / (max - min || 1) * (h - 6)]);
  const d = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join("");
  const last = pts[pts.length - 1];
  return (
    <svg className="px-spark" width={w} height={h} viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" aria-hidden>
      <path d={`${d}L${last[0]},${h}L2,${h}Z`} fill={color} opacity={0.12} />
      <path d={d} fill="none" stroke={color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={last[0]} cy={last[1]} r={3} fill={color} stroke="#141417" strokeWidth={2} />
    </svg>
  );
}

/* ── stacked hourly response time (CPU / DB / wait / other) + steps strip ── */
const PARTS: [keyof typeof SERIES, string][] = [["cpu", "CPU"], ["db", "DB"], ["wait", "Wait"], ["other", "Other"]];

export function HourlyStack({ data, target = 1000 }: { data: HourPoint[]; target?: number }) {
  const t = useTip();
  const W = 760, H = 250, padL = 44, padB = 22, padT = 12, stripH = 46;
  const plotH = H - padT - padB;
  const max = Math.max(target * 1.15, ...data.map(d => d.cpu + d.db + d.wait + d.other), 1);
  const nice = Math.ceil(max / 500) * 500;
  const bw = (W - padL - 8) / Math.max(data.length, 1);
  const y = (v: number) => padT + plotH - v / nice * plotH;
  const maxSteps = Math.max(...data.map(d => d.steps), 1);
  const lab = (d: HourPoint) => (d.label ? d.label.slice(-5, -3) : String(d.hour).padStart(2, "0"));
  const ticks = [0, nice / 2, nice];
  return (
    <div ref={t.box} className="px-chart" onMouseLeave={t.hide}>
      <div className="px-legend" aria-label="Legend">
        {PARTS.map(([k, n]) => <span key={k}><i style={{ background: SERIES[k] }} />{n}</span>)}
        <span><i className="px-dash" />Target {target} ms</span>
      </div>
      <svg viewBox={`0 0 ${W} ${H + stripH}`} width="100%" role="img"
        aria-label="Average dialog response time per hour, split into CPU, database, wait and other time">
        {ticks.map(v => (
          <g key={v}>
            <line x1={padL} x2={W - 4} y1={y(v)} y2={y(v)} stroke="rgba(255,255,255,0.07)" />
            <text x={padL - 8} y={y(v) + 4} textAnchor="end" className="px-ax">{v >= 1000 ? `${v / 1000}s` : `${v}`}</text>
          </g>
        ))}
        <line x1={padL} x2={W - 4} y1={y(target)} y2={y(target)} stroke="rgba(255,255,255,0.45)" strokeDasharray="4 4" />
        {data.map((d, i) => {
          let acc = 0;
          const x = padL + i * bw + 2, w = Math.max(bw - 4, 2);
          const segs = PARTS.map(([k]) => {
            const v = d[k] as number;
            const y0 = y(acc), y1 = y(acc + v);
            acc += v;
            return v > 0 ? <rect key={k} x={x} y={y1} width={w} height={Math.max(y0 - y1 - (acc > v ? 2 : 0), 0)}
              fill={SERIES[k]} rx={k === "other" || acc === d.cpu + d.db + d.wait + d.other ? 0 : 0} /> : null;
          });
          const top = y(acc);
          return (
            <g key={i} onMouseMove={e => t.show(e, `${d.label ?? `${lab(d)}:00`} · ${d.dia_steps.toLocaleString()} dialog steps`,
              [["Avg response", `${d.avg_resp.toLocaleString()} ms`],
               ...PARTS.map(([k, n]) => [n, `${(d[k] as number).toLocaleString()} ms`, SERIES[k]] as [string, string, string]),
               ["All steps", d.steps.toLocaleString()]])}>
              <rect x={x - 2} y={padT} width={bw} height={plotH + stripH + padB} fill="transparent" />
              {segs}
              {acc > 0 ? <rect x={x} y={top} width={w} height={4} rx={2} fill="transparent" /> : null}
              <rect x={x} y={H + stripH - 4 - (d.steps / maxSteps) * (stripH - 12)} width={w}
                height={(d.steps / maxSteps) * (stripH - 12)} rx={2} fill="rgba(235,235,245,0.28)" />
              {(i % 3 === 0 || data.length <= 12) &&
                <text x={x + w / 2} y={H - 4} textAnchor="middle" className="px-ax">{lab(d)}</text>}
            </g>
          );
        })}
        <text x={padL - 8} y={H + 18} textAnchor="end" className="px-ax">steps</text>
      </svg>
      {t.view}
    </div>
  );
}

/* ── single-axis percentage lines (HANA CPU / memory) ── */
export function PctLines({ series, labels }: { series: { name: string; color: string; data: (number | null)[] }[]; labels: string[] }) {
  const t = useTip();
  const W = 520, H = 180, padL = 34, padB = 20, padT = 10;
  const plotH = H - padT - padB, n = labels.length;
  const x = (i: number) => padL + (n <= 1 ? 0 : i / (n - 1) * (W - padL - 8));
  const y = (v: number) => padT + plotH - Math.min(v, 100) / 100 * plotH;
  return (
    <div ref={t.box} className="px-chart" onMouseLeave={t.hide}>
      <div className="px-legend">{series.map(s => <span key={s.name}><i style={{ background: s.color }} />{s.name}</span>)}</div>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img" aria-label={series.map(s => s.name).join(" and ") + " by hour"}>
        {[0, 50, 100].map(v => <g key={v}>
          <line x1={padL} x2={W - 4} y1={y(v)} y2={y(v)} stroke="rgba(255,255,255,0.07)" />
          <text x={padL - 6} y={y(v) + 4} textAnchor="end" className="px-ax">{v}%</text></g>)}
        <line x1={padL} x2={W - 4} y1={y(80)} y2={y(80)} stroke="rgba(255,255,255,0.4)" strokeDasharray="4 4" />
        {series.map(s => (
          <path key={s.name} fill="none" stroke={s.color} strokeWidth={2} strokeLinejoin="round"
            d={s.data.map((v, i) => v == null ? "" : `${i && s.data[i - 1] != null ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("")} />
        ))}
        {labels.map((l, i) => (
          <g key={i} onMouseMove={e => t.show(e, `${l}:00`, series.map(s => [s.name, s.data[i] == null ? "no data" : `${s.data[i]} %`, s.color] as [string, string, string]))}>
            <rect x={x(i) - (W / n) / 2} y={padT} width={W / n} height={plotH} fill="transparent" />
            {(i % 3 === 0) && <text x={x(i)} y={H - 4} textAnchor="middle" className="px-ax">{l}</text>}
          </g>
        ))}
        {series.map(s => {
          const i = s.data.length - 1, v = s.data[i];
          return v != null ? <circle key={s.name} cx={x(i)} cy={y(v)} r={4} fill={s.color} stroke="#141417" strokeWidth={2} /> : null;
        })}
        {series.flatMap(s => s.data.map((v, i) => v != null && s.data[i - 1] == null && s.data[i + 1] == null && i !== s.data.length - 1
          ? <circle key={`${s.name}-${i}`} cx={x(i)} cy={y(v)} r={2.5} fill={s.color} /> : null))}
      </svg>
      {t.view}
    </div>
  );
}

/* ── horizontal meter ── */
export function Meter({ label, value, max = 100, unit = "%", rating, sub }: {
  label: string; value: number | null; max?: number; unit?: string; rating: Rating; sub?: string;
}) {
  const frac = value == null ? 0 : Math.min(value / max, 1);
  return (
    <div className="px-meter">
      <div className="px-meter-h"><span>{label}</span><b>{value == null ? "–" : `${value.toLocaleString()}${unit}`}</b></div>
      <div className="px-meter-t" role="meter" aria-valuenow={value ?? undefined} aria-valuemin={0} aria-valuemax={max} aria-label={label}>
        <div style={{ width: `${frac * 100}%`, background: STATUS[rating] }} />
      </div>
      {sub ? <div className="px-meter-s">{sub}</div> : null}
    </div>
  );
}

/* ── work-process bars per type ── */
export function WpBars({ wp }: { wp: Record<string, { total: number; running: number; waiting: number; hold: number; stopped: number }> }) {
  const t = useTip();
  const NAMES: Record<string, string> = { DIA: "Dialog", BTC: "Background", UPD: "Update", UP2: "Update 2", SPO: "Spool", ENQ: "Enqueue" };
  const rows = Object.entries(wp).sort((a, b) => b[1].total - a[1].total);
  const PART: [keyof WpCountKeys, string, string][] = [["running", "Running", SERIES.cpu], ["hold", "On hold", SERIES.other],
    ["stopped", "Stopped", "#d03b3b"], ["waiting", "Free", "rgba(255,255,255,0.12)"]];
  return (
    <div ref={t.box} className="px-chart px-wp" onMouseLeave={t.hide}>
      <div className="px-legend">{PART.slice(0, 3).map(([k, n, c]) => <span key={k}><i style={{ background: c }} />{n}</span>)}
        <span><i style={{ background: "rgba(255,255,255,0.12)" }} />Free</span></div>
      {rows.map(([typ, b]) => (
        <div key={typ} className="px-wp-row" onMouseMove={e => t.show(e, `${NAMES[typ] ?? typ} work processes`,
          PART.map(([k, n, c]) => [n, String(b[k]), c] as [string, string, string]))}>
          <span className="px-wp-n">{NAMES[typ] ?? typ}</span>
          <div className="px-wp-bar">
            {PART.map(([k, , c]) => b[k] ? <div key={k} style={{ flex: b[k], background: c }} /> : null)}
          </div>
          <span className="px-wp-v">{b.running}/{b.total}</span>
        </div>
      ))}
      {t.view}
    </div>
  );
}
type WpCountKeys = { running: number; waiting: number; hold: number; stopped: number };
