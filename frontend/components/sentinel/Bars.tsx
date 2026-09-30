import type { Chart } from "@/lib/sentinelApi";

export default function Bars({ chart }: { chart: Chart }) {
  const max = Math.max(...chart.items.map(i => i.value), chart.unit === "%" ? 100 : 0, 1);
  const fmt = (v: number) => `${Number.isInteger(v) ? v.toLocaleString() : v.toFixed(1)}${chart.unit === "%" ? "%" : ""}`;
  return (
    <figure className="sx-block" style={{ margin: 0 }}>
      <h3>{chart.title}{chart.unit && chart.unit !== "%" ? <span className="sx-count"> in {chart.unit}</span> : null}</h3>
      <div className="sx-bars">
        {chart.items.map((it, i) => (
          <div className="sx-bar-row" key={i} title={it.note || it.label}>
            <span className="sx-bar-label">{it.label}{it.note ? <span style={{ color: "var(--text-3)", marginLeft: 8 }}>{it.note}</span> : null}</span>
            <span className="sx-bar-track"><span className={`sx-bar-fill sx-tone-${it.tone}`} style={{ width: `${Math.max(2, (it.value / max) * 100)}%`, display: "block" }} /></span>
            <span className="sx-bar-val">{fmt(it.value)}</span>
          </div>
        ))}
      </div>
    </figure>
  );
}
